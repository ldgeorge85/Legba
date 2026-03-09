"""
OpenSearch Agent Tools

Index management, document CRUD, full-text search, and aggregations.
Wired to the live OpenSearchStore by cycle.py.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ....shared.schemas.tools import ToolDefinition, ToolParameter

if TYPE_CHECKING:
    from ...memory.opensearch import OpenSearchStore
    from ...tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

OS_CREATE_INDEX_DEF = ToolDefinition(
    name="os_create_index",
    description="Create an OpenSearch index with optional mappings and settings. "
                "Use for storing collected data, time series, documents, analytical outputs.",
    parameters=[
        ToolParameter(name="index", type="string",
                      description="Index name (convention: legba-{purpose}-{YYYY.MM})"),
        ToolParameter(name="mappings", type="string",
                      description="Optional JSON mappings object (e.g. "
                                  '{\"properties\": {\"title\": {\"type\": \"text\"}, '
                                  '"timestamp": {\"type\": "date\"}}})',
                      required=False),
        ToolParameter(name="settings", type="string",
                      description="Optional JSON settings object (e.g. "
                                  '{\"number_of_shards\": 1, \"number_of_replicas\": 0})',
                      required=False),
    ],
)

OS_INDEX_DOCUMENT_DEF = ToolDefinition(
    name="os_index_document",
    description="Index one or more documents into an OpenSearch index. "
                "Pass a single JSON object or an array of objects for bulk indexing.",
    parameters=[
        ToolParameter(name="index", type="string",
                      description="Target index name"),
        ToolParameter(name="document", type="string",
                      description="JSON document or JSON array of documents to index"),
        ToolParameter(name="id", type="string",
                      description="Optional document ID (single doc only)",
                      required=False),
    ],
)

OS_SEARCH_DEF = ToolDefinition(
    name="os_search",
    description="Search an OpenSearch index using full-text search, term queries, "
                "bool queries, range filters, etc. Returns matching documents.",
    parameters=[
        ToolParameter(name="index", type="string",
                      description="Index name or pattern (e.g. legba-cves-*)"),
        ToolParameter(name="query", type="string",
                      description='JSON query object (e.g. {"match": {"title": "apache"}} '
                                  'or {"bool": {"must": [...], "filter": [...]}})'),
        ToolParameter(name="size", type="number",
                      description="Max documents to return (default 10)",
                      required=False),
        ToolParameter(name="sort", type="string",
                      description='Optional JSON sort array (e.g. [{"timestamp": "desc"}])',
                      required=False),
        ToolParameter(name="fields", type="string",
                      description="Optional comma-separated list of fields to return",
                      required=False),
    ],
)

OS_AGGREGATE_DEF = ToolDefinition(
    name="os_aggregate",
    description="Run aggregations on an OpenSearch index. Supports terms, date_histogram, "
                "stats, avg, sum, cardinality, and nested aggregations.",
    parameters=[
        ToolParameter(name="index", type="string",
                      description="Index name or pattern"),
        ToolParameter(name="aggs", type="string",
                      description='JSON aggregations object (e.g. '
                                  '{"severity_counts": {"terms": {"field": "severity"}}})'),
        ToolParameter(name="query", type="string",
                      description="Optional JSON query to filter documents before aggregating",
                      required=False),
    ],
)

OS_DELETE_INDEX_DEF = ToolDefinition(
    name="os_delete_index",
    description="Delete an OpenSearch index and all its documents.",
    parameters=[
        ToolParameter(name="index", type="string",
                      description="Index name to delete"),
    ],
)

OS_LIST_INDICES_DEF = ToolDefinition(
    name="os_list_indices",
    description="List OpenSearch indices with document counts and sizes. "
                "Supports wildcard patterns.",
    parameters=[
        ToolParameter(name="pattern", type="string",
                      description="Index pattern (default: legba-*)",
                      required=False),
    ],
)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register(registry: ToolRegistry, *, opensearch: OpenSearchStore) -> None:
    """Register all OpenSearch tools with the given registry."""

    def _check_available() -> str | None:
        """Return error string if OpenSearch is not available, None otherwise."""
        if opensearch is None:
            return "Error: OpenSearch store is not initialized"
        if not opensearch.available:
            return "Error: OpenSearch is not connected"
        return None

    async def create_index_handler(args: dict) -> str:
        err = _check_available()
        if err:
            return err
        index = args.get("index", "")
        if not index:
            return "Error: index name is required"
        mappings = None
        if args.get("mappings"):
            try:
                mappings = json.loads(args["mappings"])
            except json.JSONDecodeError:
                return "Error: invalid JSON in mappings"
        settings = None
        if args.get("settings"):
            try:
                settings = json.loads(args["settings"])
            except json.JSONDecodeError:
                return "Error: invalid JSON in settings"
        result = await opensearch.create_index(index, mappings=mappings, settings=settings)
        return json.dumps(result, indent=2)

    async def index_document_handler(args: dict) -> str:
        err = _check_available()
        if err:
            return err
        index = args.get("index", "")
        if not index:
            return "Error: index name is required"
        doc_str = args.get("document", "")
        if not doc_str:
            return "Error: document is required"
        try:
            parsed = json.loads(doc_str)
        except json.JSONDecodeError:
            return "Error: invalid JSON in document"

        # Array = bulk, object = single
        if isinstance(parsed, list):
            result = await opensearch.bulk_index(index, parsed)
        else:
            doc_id = args.get("id")
            result = await opensearch.index_document(index, parsed, doc_id=doc_id)
        return json.dumps(result, indent=2)

    async def search_handler(args: dict) -> str:
        err = _check_available()
        if err:
            return err
        index = args.get("index", "")
        if not index:
            return "Error: index name is required"
        query_str = args.get("query", "")
        if not query_str:
            return "Error: query is required"
        try:
            query = json.loads(query_str)
        except json.JSONDecodeError:
            return "Error: invalid JSON in query"
        size = int(args.get("size", 10))
        sort = None
        if args.get("sort"):
            try:
                sort = json.loads(args["sort"])
            except json.JSONDecodeError:
                pass
        source = None
        if args.get("fields"):
            source = [f.strip() for f in args["fields"].split(",") if f.strip()]
        result = await opensearch.search(index, query, size=size, sort=sort, source=source)
        return json.dumps(result, indent=2, default=str)

    async def aggregate_handler(args: dict) -> str:
        err = _check_available()
        if err:
            return err
        index = args.get("index", "")
        if not index:
            return "Error: index name is required"
        aggs_str = args.get("aggs", "")
        if not aggs_str:
            return "Error: aggs is required"
        try:
            aggs = json.loads(aggs_str)
        except json.JSONDecodeError:
            return "Error: invalid JSON in aggs"
        query = None
        if args.get("query"):
            try:
                query = json.loads(args["query"])
            except json.JSONDecodeError:
                return "Error: invalid JSON in query"
        result = await opensearch.aggregate(index, aggs, query=query)
        return json.dumps(result, indent=2, default=str)

    async def delete_index_handler(args: dict) -> str:
        err = _check_available()
        if err:
            return err
        index = args.get("index", "")
        if not index:
            return "Error: index name is required"
        result = await opensearch.delete_index(index)
        return json.dumps(result, indent=2)

    async def list_indices_handler(args: dict) -> str:
        err = _check_available()
        if err:
            return err
        pattern = args.get("pattern", "legba-*")
        indices = await opensearch.list_indices(pattern=pattern)
        if not indices:
            return f"No indices matching '{pattern}'"
        return json.dumps(indices, indent=2)

    registry.register(OS_CREATE_INDEX_DEF, create_index_handler)
    registry.register(OS_INDEX_DOCUMENT_DEF, index_document_handler)
    registry.register(OS_SEARCH_DEF, search_handler)
    registry.register(OS_AGGREGATE_DEF, aggregate_handler)
    registry.register(OS_DELETE_INDEX_DEF, delete_index_handler)
    registry.register(OS_LIST_INDICES_DEF, list_indices_handler)
