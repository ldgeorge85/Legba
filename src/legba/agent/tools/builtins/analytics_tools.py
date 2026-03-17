"""
Analytical Agent Tools

Statistical analysis, NLP, anomaly detection, forecasting, graph analytics.
Reference-based data flow: tools accept OpenSearch/graph references or inline data.
"""

from __future__ import annotations

import json
from typing import Any, TYPE_CHECKING

from ....shared.schemas.tools import ToolDefinition, ToolParameter

if TYPE_CHECKING:
    from ...memory.opensearch import OpenSearchStore
    from ...memory.structured import StructuredStore
    from ...memory.graph import GraphStore
    from ...tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

ANOMALY_DETECT_DEF = ToolDefinition(
    name="anomaly_detect",
    description="Detect anomalies/outliers in numeric data using statistical methods (Isolation Forest, "
                "LOF, KNN). Accepts inline data or OpenSearch reference. "
                "Returns outlier indices, scores, and summary.",
    parameters=[
        ToolParameter(name="data", type="string",
                      description="JSON array of numbers or objects with numeric fields. "
                                  "Provide this OR index+query.",
                      required=False),
        ToolParameter(name="index", type="string",
                      description="OpenSearch index to fetch data from",
                      required=False),
        ToolParameter(name="query", type="string",
                      description="OpenSearch query JSON (used with index)",
                      required=False),
        ToolParameter(name="field", type="string",
                      description="Field name with numeric values (required for object data or OpenSearch)",
                      required=False),
        ToolParameter(name="method", type="string",
                      description="Detection method: iforest (default), lof, knn",
                      required=False),
        ToolParameter(name="contamination", type="number",
                      description="Expected outlier fraction, 0.01-0.5 (default 0.1)",
                      required=False),
    ],
)

FORECAST_DEF = ToolDefinition(
    name="forecast",
    description="Forecast future values of a time series using AutoARIMA. "
                "Accepts inline data or OpenSearch reference. "
                "Returns predicted values with confidence intervals.",
    parameters=[
        ToolParameter(name="data", type="string",
                      description="JSON array of numbers (evenly spaced) or "
                                  "[{timestamp, value}, ...] objects. "
                                  "Provide this OR index+query.",
                      required=False),
        ToolParameter(name="index", type="string",
                      description="OpenSearch index to fetch time series from",
                      required=False),
        ToolParameter(name="query", type="string",
                      description="OpenSearch query JSON (used with index)",
                      required=False),
        ToolParameter(name="time_field", type="string",
                      description="Timestamp field name (default: timestamp)",
                      required=False),
        ToolParameter(name="value_field", type="string",
                      description="Value field name (default: value)",
                      required=False),
        ToolParameter(name="horizon", type="number",
                      description="Periods to forecast ahead (default 10)",
                      required=False),
        ToolParameter(name="frequency", type="string",
                      description="Data frequency: h (hourly), D (daily), W (weekly), "
                                  "MS (monthly). Auto-detected if omitted.",
                      required=False),
    ],
)

NLP_EXTRACT_DEF = ToolDefinition(
    name="nlp_extract",
    description="Extract structured information from text using spaCy NLP. "
                "Returns named entities (people, orgs, locations, etc.), "
                "noun chunks, and sentence boundaries.",
    parameters=[
        ToolParameter(name="text", type="string",
                      description="Text to process. Provide this OR index+query.",
                      required=False),
        ToolParameter(name="index", type="string",
                      description="OpenSearch index to fetch text from",
                      required=False),
        ToolParameter(name="query", type="string",
                      description="OpenSearch query JSON (used with index)",
                      required=False),
        ToolParameter(name="text_field", type="string",
                      description="Field containing text in OpenSearch docs (default: content)",
                      required=False),
        ToolParameter(name="operations", type="string",
                      description="Comma-separated: entities, noun_chunks, sentences "
                                  "(default: entities,noun_chunks)",
                      required=False),
    ],
)

GRAPH_ANALYZE_DEF = ToolDefinition(
    name="graph_analyze",
    description="Analyze graph structure using NetworkX. Load from AGE entity graph "
                "or provide inline nodes/edges. Supports centrality, PageRank, "
                "community detection, shortest paths, degree distribution.",
    parameters=[
        ToolParameter(name="entity", type="string",
                      description="Entity name to load subgraph around from AGE. "
                                  "Provide this OR nodes+edges for inline data.",
                      required=False),
        ToolParameter(name="depth", type="number",
                      description="Subgraph depth around entity (default 2, used with entity)",
                      required=False),
        ToolParameter(name="nodes", type="string",
                      description="JSON array of [{id, label, ...}] for inline graph",
                      required=False),
        ToolParameter(name="edges", type="string",
                      description="JSON array of [{source, target, label, ...}] for inline graph",
                      required=False),
        ToolParameter(name="operation", type="string",
                      description="Analysis: centrality, pagerank, community, "
                                  "shortest_path, degree, components (default: centrality)"),
        ToolParameter(name="params", type="string",
                      description="JSON params. centrality: {type: degree|betweenness|closeness}. "
                                  "shortest_path: {source, target}.",
                      required=False),
    ],
)

CORRELATE_DEF = ToolDefinition(
    name="correlate",
    description="Compute correlations, cluster data, or reduce dimensions (PCA). "
                "Accepts tabular data inline or from OpenSearch.",
    parameters=[
        ToolParameter(name="data", type="string",
                      description="JSON array of objects with numeric fields. "
                                  "Provide this OR index+query.",
                      required=False),
        ToolParameter(name="index", type="string",
                      description="OpenSearch index to fetch data from",
                      required=False),
        ToolParameter(name="query", type="string",
                      description="OpenSearch query JSON (used with index)",
                      required=False),
        ToolParameter(name="fields", type="string",
                      description="Comma-separated field names to analyze (required)"),
        ToolParameter(name="operation", type="string",
                      description="Analysis: correlation (default), cluster, pca",
                      required=False),
        ToolParameter(name="params", type="string",
                      description="JSON params. cluster: {method: kmeans|dbscan, n_clusters: N}. "
                                  "pca: {n_components: N}.",
                      required=False),
    ],
)

TEMPORAL_QUERY_DEF = ToolDefinition(
    name="temporal_query",
    description="Query event trends over time periods. Returns event counts bucketed "
                "by day/week/month, enabling trend detection and temporal pattern analysis. "
                "Filter by category, entity name, or keyword.",
    parameters=[
        ToolParameter(name="period", type="string",
                      description="Time period to analyze: 7d, 14d, 30d, 90d, 180d, 365d (default: 30d)"),
        ToolParameter(name="bucket", type="string",
                      description="Bucket size: day, week, month (default: day)",
                      required=False),
        ToolParameter(name="category", type="string",
                      description="Filter by event category",
                      required=False),
        ToolParameter(name="keyword", type="string",
                      description="Filter by keyword in event title",
                      required=False),
        ToolParameter(name="entity", type="string",
                      description="Filter by entity name (matches actors/locations in event data)",
                      required=False),
    ],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _fetch_os_data(
    opensearch: OpenSearchStore,
    index: str,
    query_str: str | None,
    fields: list[str] | None = None,
    size: int = 1000,
) -> list[dict[str, Any]]:
    """Fetch documents from OpenSearch by index + query."""
    query: dict[str, Any] = {"match_all": {}}
    if query_str:
        try:
            query = json.loads(query_str)
        except json.JSONDecodeError:
            raise ValueError("Invalid JSON in query")
    result = await opensearch.search(index, query, size=size, source=fields)
    if result.get("error"):
        raise ValueError(f"OpenSearch error: {result['error']}")
    return result.get("hits", [])


def _extract_numeric_values(data: list, field: str | None = None) -> list[float]:
    """Extract numeric values from a list of numbers or objects."""
    values = []
    for item in data:
        if isinstance(item, (int, float)):
            values.append(float(item))
        elif isinstance(item, dict) and field:
            v = item.get(field)
            if v is not None:
                try:
                    values.append(float(v))
                except (ValueError, TypeError):
                    pass
    return values


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register(
    registry: ToolRegistry,
    *,
    opensearch: OpenSearchStore,
    graph: GraphStore | None = None,
    structured: StructuredStore | None = None,
) -> None:
    """Register all analytics tools with the given registry."""

    # ---------------------------------------------------------------
    # anomaly_detect
    # ---------------------------------------------------------------
    async def anomaly_detect_handler(args: dict) -> str:
        try:
            import numpy as np
            from pyod.models.iforest import IForest
        except ImportError as e:
            return f"Error: analytics dependency not available: {e}"

        data_str = args.get("data")
        index = args.get("index")
        field = args.get("field")

        if not data_str and not index:
            return "Error: provide 'data' (inline JSON) or 'index' (OpenSearch reference)"

        try:
            if data_str:
                raw_data = json.loads(data_str)
            else:
                raw_data = await _fetch_os_data(
                    opensearch, index, args.get("query"),
                    fields=[field] if field else None,
                )
        except (json.JSONDecodeError, ValueError) as e:
            return f"Error: {e}"

        values = _extract_numeric_values(raw_data, field)
        if len(values) < 10:
            return f"Error: need at least 10 data points, got {len(values)}"

        method = args.get("method", "iforest").lower()
        contamination = float(args.get("contamination", 0.1))
        contamination = max(0.01, min(0.5, contamination))

        X = np.array(values).reshape(-1, 1)

        try:
            if method == "lof":
                from pyod.models.lof import LOF
                model = LOF(contamination=contamination)
            elif method == "knn":
                from pyod.models.knn import KNN
                model = KNN(contamination=contamination)
            else:
                model = IForest(contamination=contamination, random_state=42)

            model.fit(X)
        except Exception as e:
            return f"Error fitting model: {e}"

        outlier_indices = [int(i) for i, lbl in enumerate(model.labels_) if lbl == 1]
        outlier_values = [values[i] for i in outlier_indices]
        scores = model.decision_scores_.tolist()

        return json.dumps({
            "total_points": len(values),
            "outliers_found": len(outlier_indices),
            "method": method,
            "contamination": contamination,
            "outlier_indices": outlier_indices[:100],
            "outlier_values": outlier_values[:100],
            "score_stats": {
                "min": round(float(min(scores)), 4),
                "max": round(float(max(scores)), 4),
                "mean": round(float(np.mean(scores)), 4),
                "threshold": round(float(model.threshold_), 4),
            },
        }, indent=2)

    # ---------------------------------------------------------------
    # forecast
    # ---------------------------------------------------------------
    async def forecast_handler(args: dict) -> str:
        try:
            import numpy as np
            import pandas as pd
            from statsforecast import StatsForecast
            from statsforecast.models import AutoARIMA
        except ImportError as e:
            return f"Error: analytics dependency not available: {e}"

        data_str = args.get("data")
        index = args.get("index")
        time_field = args.get("time_field", "timestamp")
        value_field = args.get("value_field", "value")
        horizon = int(args.get("horizon", 10))
        freq = args.get("frequency")

        if not data_str and not index:
            return "Error: provide 'data' (inline JSON) or 'index' (OpenSearch reference)"

        try:
            if data_str:
                raw_data = json.loads(data_str)
            else:
                raw_data = await _fetch_os_data(
                    opensearch, index, args.get("query"),
                    fields=[time_field, value_field],
                )
        except (json.JSONDecodeError, ValueError) as e:
            return f"Error: {e}"

        # Build time series
        if raw_data and isinstance(raw_data[0], (int, float)):
            values = [float(v) for v in raw_data]
            dates = pd.date_range("2020-01-01", periods=len(values), freq=freq or "D")
        else:
            values = []
            dates = []
            for item in raw_data:
                v = item.get(value_field)
                t = item.get(time_field)
                if v is not None and t is not None:
                    try:
                        values.append(float(v))
                        dates.append(pd.Timestamp(t))
                    except (ValueError, TypeError):
                        pass

        if len(values) < 5:
            return f"Error: need at least 5 data points, got {len(values)}"

        # Detect frequency if not specified
        if not freq and len(dates) >= 2:
            freq = pd.infer_freq(pd.DatetimeIndex(sorted(dates))) or "D"

        df = pd.DataFrame({
            "unique_id": ["ts"] * len(values),
            "ds": dates[:len(values)],
            "y": values,
        }).sort_values("ds")

        try:
            sf = StatsForecast(models=[AutoARIMA()], freq=freq or "D")
            forecast_df = sf.forecast(h=horizon, df=df)
        except Exception as e:
            return f"Error forecasting: {e}"

        predictions = []
        for _, row in forecast_df.iterrows():
            pred: dict[str, Any] = {"value": round(float(row.get("AutoARIMA", 0)), 4)}
            if "AutoARIMA-lo-95" in row:
                pred["lower_95"] = round(float(row["AutoARIMA-lo-95"]), 4)
            if "AutoARIMA-hi-95" in row:
                pred["upper_95"] = round(float(row["AutoARIMA-hi-95"]), 4)
            predictions.append(pred)

        return json.dumps({
            "input_points": len(values),
            "horizon": horizon,
            "frequency": freq or "D",
            "predictions": predictions,
        }, indent=2)

    # ---------------------------------------------------------------
    # nlp_extract
    # ---------------------------------------------------------------
    async def nlp_extract_handler(args: dict) -> str:
        try:
            import spacy
        except ImportError:
            return "Error: spaCy not available"

        text = args.get("text")
        index = args.get("index")
        text_field = args.get("text_field", "content")
        operations = [
            op.strip()
            for op in args.get("operations", "entities,noun_chunks").split(",")
        ]

        if not text and not index:
            return "Error: provide 'text' or 'index' (OpenSearch reference)"

        if not text:
            try:
                hits = await _fetch_os_data(
                    opensearch, index, args.get("query"),
                    fields=[text_field], size=50,
                )
                texts = [h.get(text_field, "") for h in hits if h.get(text_field)]
                text = "\n\n".join(texts)
            except ValueError as e:
                return f"Error: {e}"

        if not text:
            return "Error: no text found to process"

        text = text[:50000]  # cap processing size

        try:
            nlp = spacy.load("en_core_web_sm")
        except OSError:
            return ("Error: spaCy model 'en_core_web_sm' not installed. "
                    "Run: python -m spacy download en_core_web_sm")

        doc = nlp(text)
        result: dict[str, Any] = {"text_length": len(text)}

        if "entities" in operations:
            entities: dict[str, list[str]] = {}
            for ent in doc.ents:
                label = ent.label_
                if label not in entities:
                    entities[label] = []
                if ent.text not in entities[label]:
                    entities[label].append(ent.text)
            result["entities"] = entities
            result["entity_count"] = sum(len(v) for v in entities.values())

        if "noun_chunks" in operations:
            chunks = list(set(chunk.text.strip() for chunk in doc.noun_chunks))
            result["noun_chunks"] = sorted(chunks)[:100]

        if "sentences" in operations:
            sents = [sent.text.strip() for sent in doc.sents]
            result["sentences"] = sents[:100]
            result["sentence_count"] = len(sents)

        return json.dumps(result, indent=2, default=str)

    # ---------------------------------------------------------------
    # graph_analyze
    # ---------------------------------------------------------------
    async def graph_analyze_handler(args: dict) -> str:
        try:
            import networkx as nx
        except ImportError:
            return "Error: NetworkX not available"

        entity = args.get("entity")
        nodes_str = args.get("nodes")
        edges_str = args.get("edges")
        operation = args.get("operation", "centrality").lower()
        params_str = args.get("params")
        params: dict[str, Any] = {}
        if params_str:
            try:
                params = json.loads(params_str)
            except json.JSONDecodeError:
                return "Error: invalid JSON in params"

        G = nx.DiGraph()

        # Build graph from AGE subgraph or inline data
        if entity:
            if not graph or not graph.available:
                return "Error: entity graph (AGE) not available"
            depth = int(args.get("depth", 2))
            try:
                subgraph = await graph.query_subgraph(entity, depth=depth)
                for v in subgraph.get("entities", []):
                    nid = v.get("name", "")
                    G.add_node(nid, type=v.get("type", ""), **v.get("properties", {}))
                for e in subgraph.get("relationships", []):
                    G.add_edge(
                        e.get("source", ""), e.get("target", ""),
                        label=e.get("relation", ""),
                        **e.get("properties", {}),
                    )
            except Exception as e:
                return f"Error loading graph from AGE: {e}"
        elif nodes_str or edges_str:
            if nodes_str:
                try:
                    nodes = json.loads(nodes_str)
                    for n in nodes:
                        nid = str(n.get("id", ""))
                        G.add_node(nid, **{k: v for k, v in n.items() if k != "id"})
                except json.JSONDecodeError:
                    return "Error: invalid JSON in nodes"
            if edges_str:
                try:
                    edges = json.loads(edges_str)
                    for e in edges:
                        G.add_edge(
                            str(e.get("source", "")), str(e.get("target", "")),
                            **{k: v for k, v in e.items() if k not in ("source", "target")},
                        )
                except json.JSONDecodeError:
                    return "Error: invalid JSON in edges"
        else:
            return "Error: provide 'entity' (AGE subgraph) or 'nodes'+'edges' (inline)"

        if G.number_of_nodes() == 0:
            return "Error: graph has no nodes"

        result_data: dict[str, Any] = {
            "nodes": G.number_of_nodes(),
            "edges": G.number_of_edges(),
            "operation": operation,
        }

        try:
            if operation == "centrality":
                c_type = params.get("type", "degree")
                if c_type == "betweenness":
                    scores = nx.betweenness_centrality(G)
                elif c_type == "closeness":
                    scores = nx.closeness_centrality(G)
                else:
                    scores = nx.degree_centrality(G)
                sorted_nodes = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:20]
                result_data["centrality_type"] = c_type
                result_data["top_nodes"] = [
                    {"node": n, "score": round(s, 4)} for n, s in sorted_nodes
                ]

            elif operation == "pagerank":
                scores = nx.pagerank(G)
                sorted_nodes = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:20]
                result_data["top_nodes"] = [
                    {"node": n, "score": round(s, 6)} for n, s in sorted_nodes
                ]

            elif operation == "community":
                UG = G.to_undirected()
                from networkx.algorithms.community import greedy_modularity_communities
                communities = list(greedy_modularity_communities(UG))
                result_data["communities"] = [
                    {"id": i, "size": len(c), "members": sorted(list(c))[:50]}
                    for i, c in enumerate(communities)
                ]
                result_data["num_communities"] = len(communities)

            elif operation == "shortest_path":
                source = str(params.get("source", ""))
                target = str(params.get("target", ""))
                if not source or not target:
                    return "Error: shortest_path requires params.source and params.target"
                try:
                    path = nx.shortest_path(G, source, target)
                    result_data["path"] = path
                    result_data["length"] = len(path) - 1
                except nx.NetworkXNoPath:
                    result_data["path"] = None
                    result_data["error"] = "No path exists"
                except nx.NodeNotFound as e:
                    return f"Error: {e}"

            elif operation == "degree":
                degrees = dict(G.degree())
                sorted_nodes = sorted(degrees.items(), key=lambda x: x[1], reverse=True)[:20]
                result_data["top_nodes"] = [
                    {"node": n, "degree": d} for n, d in sorted_nodes
                ]
                result_data["avg_degree"] = round(
                    sum(degrees.values()) / max(len(degrees), 1), 2
                )

            elif operation == "components":
                if G.is_directed():
                    components = list(nx.weakly_connected_components(G))
                else:
                    components = list(nx.connected_components(G))
                result_data["num_components"] = len(components)
                result_data["component_sizes"] = sorted(
                    [len(c) for c in components], reverse=True
                )[:20]

            else:
                return (f"Error: unknown operation '{operation}'. "
                        "Use: centrality, pagerank, community, shortest_path, degree, components")
        except Exception as e:
            return f"Error during graph analysis: {e}"

        return json.dumps(result_data, indent=2, default=str)

    # ---------------------------------------------------------------
    # correlate
    # ---------------------------------------------------------------
    async def correlate_handler(args: dict) -> str:
        try:
            import numpy as np
        except ImportError:
            return "Error: numpy not available"

        data_str = args.get("data")
        index = args.get("index")
        fields_str = args.get("fields", "")
        operation = args.get("operation", "correlation").lower()
        params_str = args.get("params")
        params: dict[str, Any] = {}
        if params_str:
            try:
                params = json.loads(params_str)
            except json.JSONDecodeError:
                return "Error: invalid JSON in params"

        if not fields_str:
            return "Error: 'fields' is required (comma-separated field names)"

        fields = [f.strip() for f in fields_str.split(",") if f.strip()]

        if not data_str and not index:
            return "Error: provide 'data' (inline JSON) or 'index' (OpenSearch reference)"

        try:
            if data_str:
                raw_data = json.loads(data_str)
            else:
                raw_data = await _fetch_os_data(
                    opensearch, index, args.get("query"), fields=fields,
                )
        except (json.JSONDecodeError, ValueError) as e:
            return f"Error: {e}"

        # Extract field values
        field_data: dict[str, list[float]] = {f: [] for f in fields}
        for item in raw_data:
            if not isinstance(item, dict):
                continue
            for f in fields:
                v = item.get(f)
                try:
                    field_data[f].append(float(v) if v is not None else np.nan)
                except (ValueError, TypeError):
                    field_data[f].append(np.nan)

        min_len = min(len(v) for v in field_data.values()) if field_data else 0
        if min_len < 3:
            return f"Error: need at least 3 data points, got {min_len}"

        X = np.column_stack([np.array(field_data[f][:min_len]) for f in fields])
        mask = ~np.isnan(X).any(axis=1)
        X = X[mask]
        if len(X) < 3:
            return "Error: insufficient non-null data points"

        result_data: dict[str, Any] = {
            "data_points": len(X),
            "fields": fields,
            "operation": operation,
        }

        try:
            if operation == "correlation":
                corr = np.corrcoef(X.T)
                result_data["correlation_matrix"] = {
                    fields[i]: {
                        fields[j]: round(float(corr[i][j]), 4)
                        for j in range(len(fields))
                    }
                    for i in range(len(fields))
                }
                strong = []
                for i in range(len(fields)):
                    for j in range(i + 1, len(fields)):
                        r = float(corr[i][j])
                        if abs(r) > 0.5:
                            strong.append({
                                "field_a": fields[i],
                                "field_b": fields[j],
                                "correlation": round(r, 4),
                                "strength": "strong" if abs(r) > 0.7 else "moderate",
                            })
                result_data["notable_correlations"] = strong

            elif operation == "cluster":
                from sklearn.cluster import KMeans, DBSCAN
                from sklearn.preprocessing import StandardScaler

                X_scaled = StandardScaler().fit_transform(X)
                method = params.get("method", "kmeans")

                if method == "dbscan":
                    eps = float(params.get("eps", 0.5))
                    labels = DBSCAN(eps=eps).fit_predict(X_scaled)
                else:
                    n_clusters = int(params.get("n_clusters", 3))
                    labels = KMeans(
                        n_clusters=n_clusters, random_state=42, n_init=10,
                    ).fit_predict(X_scaled)

                clusters = []
                for label in sorted(set(labels)):
                    cluster_mask = labels == label
                    cluster_data = X[cluster_mask]
                    clusters.append({
                        "cluster": int(label),
                        "size": int(cluster_mask.sum()),
                        "centroid": {
                            fields[i]: round(float(cluster_data[:, i].mean()), 4)
                            for i in range(len(fields))
                        },
                    })

                result_data["method"] = method
                result_data["clusters"] = clusters
                result_data["num_clusters"] = len(clusters)

            elif operation == "pca":
                from sklearn.decomposition import PCA
                from sklearn.preprocessing import StandardScaler

                n_comp = min(int(params.get("n_components", 2)), len(fields))
                X_scaled = StandardScaler().fit_transform(X)
                pca = PCA(n_components=n_comp)
                pca.fit(X_scaled)

                result_data["n_components"] = n_comp
                result_data["explained_variance_ratio"] = [
                    round(float(v), 4) for v in pca.explained_variance_ratio_
                ]
                result_data["total_variance_explained"] = round(
                    float(sum(pca.explained_variance_ratio_)), 4
                )
                result_data["components"] = [
                    {fields[j]: round(float(pca.components_[i][j]), 4)
                     for j in range(len(fields))}
                    for i in range(n_comp)
                ]

            else:
                return (f"Error: unknown operation '{operation}'. "
                        "Use: correlation, cluster, pca")
        except Exception as e:
            return f"Error during analysis: {e}"

        return json.dumps(result_data, indent=2)

    # ---------------------------------------------------------------
    # temporal_query
    # ---------------------------------------------------------------
    async def temporal_query_handler(args: dict) -> str:
        if structured is None or not structured._available:
            return "Error: Structured store (Postgres) is not available"

        period_str = args.get("period", "30d").strip().lower()
        bucket = args.get("bucket", "day").strip().lower()
        category = args.get("category")
        keyword = args.get("keyword")
        entity = args.get("entity")

        # Parse period
        import re as _re
        m = _re.match(r"^(\d+)([dwm])$", period_str)
        if not m:
            return "Error: Invalid period format. Use: 7d, 14d, 30d, 90d, 180d, 365d"
        num, unit = int(m.group(1)), m.group(2)
        if unit == "w":
            num *= 7
        elif unit == "m":
            num *= 30

        if bucket not in ("day", "week", "month"):
            return "Error: bucket must be one of: day, week, month"

        from datetime import datetime, timezone, timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(days=num)

        try:
            async with structured._pool.acquire() as conn:
                # Build query dynamically
                conditions = ["e.created_at >= $1"]
                params: list = [cutoff]
                param_idx = 2

                if category:
                    conditions.append(f"e.category = ${param_idx}")
                    params.append(category)
                    param_idx += 1

                if keyword:
                    conditions.append(f"e.title ILIKE ${param_idx}")
                    params.append(f"%{keyword}%")
                    param_idx += 1

                if entity:
                    conditions.append(f"e.data::text ILIKE ${param_idx}")
                    params.append(f"%{entity}%")
                    param_idx += 1

                where = " AND ".join(conditions)

                # Bucketed counts
                rows = await conn.fetch(f"""
                    SELECT date_trunc('{bucket}', e.created_at) AS bucket_start,
                           count(*) AS event_count
                    FROM signals e
                    WHERE {where}
                    GROUP BY bucket_start
                    ORDER BY bucket_start
                """, *params)

                buckets = [
                    {
                        "period": row["bucket_start"].isoformat(),
                        "count": row["event_count"],
                    }
                    for row in rows
                ]

                # Category breakdown
                cat_rows = await conn.fetch(f"""
                    SELECT category, count(*) AS cnt
                    FROM signals e
                    WHERE {where}
                    GROUP BY category
                    ORDER BY cnt DESC
                """, *params)

                categories_breakdown = {
                    r["category"]: r["cnt"] for r in cat_rows
                }

                total = sum(b["count"] for b in buckets)
                avg_per_bucket = round(total / max(len(buckets), 1), 1)

                # Trend detection: compare first half vs second half
                trend = "stable"
                if len(buckets) >= 4:
                    mid = len(buckets) // 2
                    first_half = sum(b["count"] for b in buckets[:mid])
                    second_half = sum(b["count"] for b in buckets[mid:])
                    if second_half > first_half * 1.3:
                        trend = "increasing"
                    elif second_half < first_half * 0.7:
                        trend = "decreasing"

                result = {
                    "period": period_str,
                    "bucket_size": bucket,
                    "total_events": total,
                    "num_buckets": len(buckets),
                    "avg_per_bucket": avg_per_bucket,
                    "trend": trend,
                    "categories": categories_breakdown,
                    "buckets": buckets,
                }

                if keyword:
                    result["keyword_filter"] = keyword
                if entity:
                    result["entity_filter"] = entity
                if category:
                    result["category_filter"] = category

                return json.dumps(result, indent=2, default=str)
        except Exception as e:
            return f"Error: temporal query failed: {e}"

    # ---------------------------------------------------------------
    # Register all
    # ---------------------------------------------------------------
    registry.register(ANOMALY_DETECT_DEF, anomaly_detect_handler)
    registry.register(FORECAST_DEF, forecast_handler)
    registry.register(NLP_EXTRACT_DEF, nlp_extract_handler)
    registry.register(GRAPH_ANALYZE_DEF, graph_analyze_handler)
    registry.register(CORRELATE_DEF, correlate_handler)
    registry.register(TEMPORAL_QUERY_DEF, temporal_query_handler)
