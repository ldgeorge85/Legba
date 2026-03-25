"""
Entity Graph Store (Apache AGE)

Stores entities and their relationships using Apache AGE, a graph database
extension for PostgreSQL that provides full Cypher query support.

Entities become labeled vertices (label = entity_type in CamelCase).
Relationships become directed edges (label = relation_type in CamelCase).
The agent can also execute raw Cypher for complex pattern matching.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import asyncpg

from ...shared.schemas.memory import Entity


class GraphStore:
    """Apache AGE-backed entity graph store with Cypher queries."""

    GRAPH_NAME = "legba_graph"

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None
        self._available = False

    @property
    def available(self) -> bool:
        return self._available

    async def connect(self) -> None:
        """Initialize AGE extension, connection pool, and graph.

        Retries up to 3 times with 2s delay to handle transient Postgres
        startup timing issues in Docker.
        """
        import asyncio
        for attempt in range(3):
            try:
                # Bootstrap: create extension with a temporary connection
                tmp = await asyncpg.connect(self._dsn)
                try:
                    await tmp.execute("CREATE EXTENSION IF NOT EXISTS age")
                finally:
                    await tmp.close()

                # Pool with per-connection codec registration (one-time per conn)
                if self._pool is None:
                    self._pool = await asyncpg.create_pool(
                        self._dsn, min_size=1, max_size=3,
                        init=self._register_codec,
                    )

                # Create graph if it doesn't exist
                async with self._pool.acquire() as conn:
                    await self._prepare(conn)
                    exists = await conn.fetchval(
                        "SELECT count(*) FROM ag_catalog.ag_graph WHERE name = $1",
                        self.GRAPH_NAME,
                    )
                    if not exists:
                        await conn.execute(
                            f"SELECT create_graph('{self.GRAPH_NAME}')"
                        )

                self._available = True
                return
            except Exception:
                if attempt < 2:
                    await asyncio.sleep(2)
                else:
                    self._available = False

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None

    @staticmethod
    async def _register_codec(conn) -> None:
        """One-time codec registration per connection (pool init callback)."""
        await conn.execute("LOAD 'age'")
        await conn.execute('SET search_path = ag_catalog, "$user", public')
        await conn.set_type_codec(
            'agtype', schema='ag_catalog',
            encoder=str, decoder=lambda x: x, format='text',
        )

    @staticmethod
    async def _prepare(conn) -> None:
        """Prepare a connection for AGE queries (must call on every acquire).

        asyncpg resets session state (SET search_path) when returning
        connections to the pool. LOAD persists but search_path does not.
        """
        await conn.execute("LOAD 'age'")
        await conn.execute('SET search_path = ag_catalog, "$user", public')

    # ------------------------------------------------------------------
    # Cypher helpers
    # ------------------------------------------------------------------

    async def _cypher(
        self, conn, query: str, cols: str = "v agtype",
    ) -> list[dict[str, Any]]:
        """Execute a Cypher query via AGE and parse the results.

        Automatically prepares the connection (search_path) before executing.
        """
        await self._prepare(conn)
        sql = f"SELECT * FROM cypher('{self.GRAPH_NAME}', $$ {query} $$) AS ({cols})"
        rows = await conn.fetch(sql)
        col_names = [c.strip().split()[0] for c in cols.split(",")]
        return [
            {name: self._parse_agtype(row[name]) for name in col_names}
            for row in rows
        ]

    @staticmethod
    def _parse_agtype(val) -> Any:
        """Parse an agtype text value into a Python object.

        AGE text representations:
          vertex:  {"id": 123, "label": "Country", "properties": {...}}::vertex
          edge:    {"id": 456, "label": "invaded", ...}::edge
          path:    [{vertex}, {edge}, {vertex}]::path
          string:  "hello"
          number:  42  or  3.14
          bool:    true / false
          null:    (None)
        """
        if val is None:
            return None
        s = str(val).strip()
        # Strip AGE type suffixes — both outer and inner.
        # Path objects contain nested ::vertex and ::edge suffixes:
        #   [{...}::vertex, {...}::edge, {...}::vertex]::path
        # Strip all of them so json.loads works.
        for suffix in ("::path", "::vertex", "::edge",
                        "::numeric", "::integer", "::float", "::boolean"):
            s = s.replace(suffix, "")
        s = s.strip()
        try:
            return json.loads(s)
        except (json.JSONDecodeError, TypeError):
            return s

    @staticmethod
    def _sanitize_label(raw: str) -> str:
        """Convert a type string to a valid Cypher label (CamelCase).

        'country'        -> 'Country'
        'military_base'  -> 'MilitaryBase'
        'OPEC'           -> 'OPEC'
        'person'         -> 'Person'
        """
        parts = re.split(r'[^a-zA-Z0-9]+', raw)
        label = "".join(
            (p[0].upper() + p[1:]) if p else ""
            for p in parts
        )
        if not label or not label[0].isalpha():
            label = "Entity" + label
        return label

    @staticmethod
    def _escape(val: str) -> str:
        """Escape a string for embedding in a Cypher single-quoted literal."""
        return val.replace("\\", "\\\\").replace("'", "\\'")

    @classmethod
    def _to_cypher_value(cls, val: Any) -> str:
        """Convert a Python value to a Cypher literal."""
        if val is None:
            return "null"
        if isinstance(val, bool):
            return "true" if val else "false"
        if isinstance(val, (int, float)):
            return str(val)
        return f"'{cls._escape(str(val))}'"

    @classmethod
    def _dict_to_cypher_map(cls, d: dict) -> str:
        """Convert a Python dict to a Cypher map literal: {k1: v1, k2: v2}."""
        items = [f"{k}: {cls._to_cypher_value(v)}" for k, v in d.items()]
        return "{" + ", ".join(items) + "}"

    def _vertex_to_entity(self, vertex: dict) -> Entity:
        """Convert an AGE vertex dict to an Entity model."""
        props = dict(vertex.get("properties", {}))
        entity_id = props.pop("entity_id", None)
        name = props.pop("name", "")
        updated = props.pop("updated_at", None)
        created = props.pop("created_at", None)

        return Entity(
            id=UUID(entity_id) if entity_id else uuid4(),
            name=name,
            entity_type=vertex.get("label", "unknown"),
            properties={k: v for k, v in props.items()},
            updated_at=(
                datetime.fromisoformat(updated)
                if updated else datetime.now(timezone.utc)
            ),
            created_at=(
                datetime.fromisoformat(created)
                if created else datetime.now(timezone.utc)
            ),
        )

    # ------------------------------------------------------------------
    # Entity operations
    # ------------------------------------------------------------------

    async def upsert_entity(self, entity: Entity) -> bool:
        """Create or update an entity as a labeled vertex.

        First checks for any existing vertex with the same name (regardless of
        label) to prevent duplicates when the entity type changes between calls.
        AGE's MERGE includes the label in the match, so a name that was first
        created as :Unknown and later referenced as :Country would create two
        nodes. This method avoids that by doing MATCH-first, CREATE-if-absent.
        """
        if not self._available:
            return False
        try:
            label = self._sanitize_label(entity.entity_type)
            name_esc = self._escape(entity.name)
            props = {**entity.properties}
            props["entity_id"] = str(entity.id)
            props["created_at"] = entity.created_at.isoformat()
            props_map = self._dict_to_cypher_map(props)
            now = datetime.now(timezone.utc).isoformat()

            async with self._pool.acquire() as conn:
                # Check if a vertex with this name already exists (any label)
                existing = await self._cypher(conn, f"""
                    MATCH (n {{name: '{name_esc}'}})
                    RETURN n
                    LIMIT 1
                """, cols="n agtype")

                if existing:
                    # Update existing vertex properties (label cannot change in AGE)
                    await self._cypher(conn, f"""
                        MATCH (n {{name: '{name_esc}'}})
                        SET n += {props_map}
                        SET n.updated_at = '{now}'
                        RETURN n
                    """, cols="n agtype")
                else:
                    # Create new vertex with the specified label
                    await self._cypher(conn, f"""
                        CREATE (n:{label} {{name: '{name_esc}'}})
                        SET n += {props_map}
                        SET n.updated_at = '{now}'
                        RETURN n
                    """, cols="n agtype")
            return True
        except Exception:
            return False

    async def find_entity(self, name: str) -> Entity | None:
        """Find an entity by exact name (case-insensitive)."""
        if not self._available:
            return None
        try:
            name_lower = self._escape(name.lower())
            async with self._pool.acquire() as conn:
                results = await self._cypher(conn, f"""
                    MATCH (n)
                    WHERE toLower(n.name) = '{name_lower}'
                    RETURN n
                    LIMIT 1
                """, cols="n agtype")
                if results:
                    return self._vertex_to_entity(results[0]["n"])
            return None
        except Exception:
            return None

    async def search_entities(
        self,
        query: str | None = None,
        entity_type: str | None = None,
        limit: int = 20,
    ) -> list[Entity]:
        """Search entities by name (fuzzy) and/or type label."""
        if not self._available:
            return []
        try:
            conditions = []
            if query:
                q_esc = self._escape(query.lower())
                conditions.append(f"toLower(n.name) CONTAINS '{q_esc}'")

            if entity_type:
                label = self._sanitize_label(entity_type)
                match_clause = f"MATCH (n:{label})"
            else:
                match_clause = "MATCH (n)"

            where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

            async with self._pool.acquire() as conn:
                results = await self._cypher(conn, f"""
                    {match_clause}
                    {where}
                    RETURN n
                    LIMIT {int(limit)}
                """, cols="n agtype")
                return [self._vertex_to_entity(r["n"]) for r in results]
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Relationship operations
    # ------------------------------------------------------------------

    async def add_relationship(
        self,
        source_name: str,
        target_name: str,
        relation_type: str,
        properties: dict[str, Any] | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> bool:
        """Create or update a directed relationship (edge) between two entities.

        Temporal properties are maintained automatically:
        - weight (float 0-1): importance/strength, default 0.5
        - confidence (float 0-1): certainty, default 0.5
        - evidence_count (int): incremented on each reinforcement
        - last_evidenced (ISO timestamp): updated on each reinforcement
        - volatility (float 0-1): tracks how often weight changes
        """
        if not self._available:
            return False
        try:
            rel_label = self._sanitize_label(relation_type)
            src_esc = self._escape(source_name)
            tgt_esc = self._escape(target_name)
            now = datetime.now(timezone.utc).isoformat()
            props = dict(properties) if properties else {}
            if since:
                props["since"] = since
            if until:
                props["until"] = until

            # Extract temporal properties from caller or use defaults
            new_weight = props.pop("weight", 0.5)
            new_confidence = props.pop("confidence", 0.5)

            props_map = self._dict_to_cypher_map(props) if props else "{}"

            async with self._pool.acquire() as conn:
                # Check if edge already exists (reinforcement path)
                existing = await self._cypher(conn, f"""
                    MATCH (a {{name: '{src_esc}'}})-[r:{rel_label}]->(b {{name: '{tgt_esc}'}})
                    RETURN r
                """, cols="r agtype")

                if existing:
                    # Reinforcement: increment evidence_count, update last_evidenced
                    old_edge = existing[0]["r"]
                    old_props = old_edge.get("properties", {}) if isinstance(old_edge, dict) else {}
                    old_count = old_props.get("evidence_count", 1)
                    old_volatility = old_props.get("volatility", 0.0)
                    old_weight = old_props.get("weight", 0.5)

                    new_count = (int(old_count) if old_count else 1) + 1
                    # If weight changed, increase volatility
                    try:
                        vol = float(old_volatility) if old_volatility else 0.0
                        ow = float(old_weight) if old_weight else 0.5
                    except (TypeError, ValueError):
                        vol = 0.0
                        ow = 0.5
                    if abs(float(new_weight) - ow) > 0.01:
                        vol = min(1.0, vol + 0.1)

                    await self._cypher(conn, f"""
                        MATCH (a {{name: '{src_esc}'}})-[r:{rel_label}]->(b {{name: '{tgt_esc}'}})
                        SET r += {props_map}
                        SET r.weight = {float(new_weight)}
                        SET r.confidence = {float(new_confidence)}
                        SET r.evidence_count = {new_count}
                        SET r.last_evidenced = '{self._escape(now)}'
                        SET r.volatility = {vol}
                        RETURN r
                    """, cols="r agtype")
                    action = "update"
                else:
                    # New edge: set defaults
                    await self._cypher(conn, f"""
                        MATCH (a {{name: '{src_esc}'}}), (b {{name: '{tgt_esc}'}})
                        MERGE (a)-[r:{rel_label}]->(b)
                        SET r += {props_map}
                        SET r.weight = {float(new_weight)}
                        SET r.confidence = {float(new_confidence)}
                        SET r.evidence_count = 1
                        SET r.last_evidenced = '{self._escape(now)}'
                        SET r.volatility = 0.0
                        RETURN r
                    """, cols="r agtype")
                    action = "create"

            # Record relationship change to TimescaleDB
            try:
                from ...shared.relationship_history import record_edge_change
                await record_edge_change(
                    source_entity=source_name,
                    target_entity=target_name,
                    rel_type=relation_type,
                    action=action,
                    weight=float(new_weight),
                    confidence=float(new_confidence),
                )
            except Exception:
                pass  # Non-fatal: metrics recording failure

            return True
        except Exception:
            return False

    async def remove_relationship(
        self,
        source_name: str,
        target_name: str,
        relation_type: str,
    ) -> bool:
        """Remove a directed relationship between two entities."""
        if not self._available:
            return False
        try:
            rel_label = self._sanitize_label(relation_type)
            src_esc = self._escape(source_name)
            tgt_esc = self._escape(target_name)
            async with self._pool.acquire() as conn:
                await self._cypher(conn, f"""
                    MATCH (a {{name: '{src_esc}'}})-[r:{rel_label}]->(b {{name: '{tgt_esc}'}})
                    DELETE r
                    RETURN count(r)
                """, cols="cnt agtype")
            return True
        except Exception:
            return False

    async def get_relationships(
        self,
        entity_name: str,
        direction: str = "both",
        relation_type: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Get relationships for an entity.

        direction: 'outgoing', 'incoming', or 'both'
        """
        if not self._available:
            return []
        try:
            name_esc = self._escape(entity_name)
            results = []

            async with self._pool.acquire() as conn:
                if direction in ("outgoing", "both"):
                    if relation_type:
                        rl = self._sanitize_label(relation_type)
                        pattern = f"(a {{name: '{name_esc}'}})-[r:{rl}]->(b)"
                    else:
                        pattern = f"(a {{name: '{name_esc}'}})-[r]->(b)"

                    rows = await self._cypher(conn, f"""
                        MATCH {pattern}
                        RETURN a, r, b
                        LIMIT {int(limit)}
                    """, cols="a agtype, r agtype, b agtype")

                    for row in rows:
                        edge = row["r"]
                        target = row["b"]
                        t_props = dict(target.get("properties", {}))
                        e_props = dict(edge.get("properties", {}))
                        results.append({
                            "direction": "outgoing",
                            "relation_type": edge.get("label", ""),
                            "rel_properties": e_props,
                            "entity_name": t_props.get("name", ""),
                            "entity_type": target.get("label", ""),
                            "entity_properties": {
                                k: v for k, v in t_props.items()
                                if k not in ("name", "entity_id", "created_at", "updated_at")
                            },
                        })

                if direction in ("incoming", "both"):
                    if relation_type:
                        rl = self._sanitize_label(relation_type)
                        pattern = f"(b)-[r:{rl}]->(a {{name: '{name_esc}'}})"
                    else:
                        pattern = f"(b)-[r]->(a {{name: '{name_esc}'}})"

                    rows = await self._cypher(conn, f"""
                        MATCH {pattern}
                        RETURN b, r, a
                        LIMIT {int(limit)}
                    """, cols="b agtype, r agtype, a agtype")

                    for row in rows:
                        edge = row["r"]
                        source = row["b"]
                        s_props = dict(source.get("properties", {}))
                        e_props = dict(edge.get("properties", {}))
                        results.append({
                            "direction": "incoming",
                            "relation_type": edge.get("label", ""),
                            "rel_properties": e_props,
                            "entity_name": s_props.get("name", ""),
                            "entity_type": source.get("label", ""),
                            "entity_properties": {
                                k: v for k, v in s_props.items()
                                if k not in ("name", "entity_id", "created_at", "updated_at")
                            },
                        })

            return results
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Graph queries
    # ------------------------------------------------------------------

    async def find_path(
        self,
        source_name: str,
        target_name: str,
        max_depth: int = 4,
    ) -> list[dict[str, Any]] | None:
        """Find a shortest path between two entities by name.

        Returns list of path steps [{source, relation, target}, ...] or None.
        """
        if not self._available:
            return None
        try:
            src_esc = self._escape(source_name)
            tgt_esc = self._escape(target_name)

            async with self._pool.acquire() as conn:
                # Variable-length path match, ordered by length
                rows = await self._cypher(conn, f"""
                    MATCH p = (a {{name: '{src_esc}'}})-[*1..{int(max_depth)}]-(b {{name: '{tgt_esc}'}})
                    RETURN p
                    LIMIT 1
                """, cols="p agtype")

                if not rows:
                    return None

                path_data = rows[0]["p"]
                return self._parse_path(path_data)
        except Exception:
            return None

    def _parse_path(self, path_data: Any) -> list[dict[str, Any]]:
        """Parse an AGE path into a list of step dicts."""
        if isinstance(path_data, list):
            # Path is [vertex, edge, vertex, edge, vertex, ...]
            steps = []
            i = 0
            while i + 2 <= len(path_data):
                v1 = path_data[i]
                edge = path_data[i + 1] if i + 1 < len(path_data) else None
                v2 = path_data[i + 2] if i + 2 < len(path_data) else None
                if edge and v2:
                    v1_props = v1.get("properties", {}) if isinstance(v1, dict) else {}
                    v2_props = v2.get("properties", {}) if isinstance(v2, dict) else {}
                    edge_label = edge.get("label", "") if isinstance(edge, dict) else str(edge)
                    steps.append({
                        "source": v1_props.get("name", "?"),
                        "relation": edge_label,
                        "target": v2_props.get("name", "?"),
                    })
                i += 2
            return steps
        # Fallback: return raw
        return [{"raw": str(path_data)}]

    async def query_subgraph(
        self,
        entity_name: str,
        depth: int = 2,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Get the subgraph around an entity up to a given depth.

        Returns {entities: [...], relationships: [...]}.
        """
        if not self._available:
            return {"entities": [], "relationships": []}
        try:
            name_esc = self._escape(entity_name)

            async with self._pool.acquire() as conn:
                # Step 1: Get all reachable nodes
                node_rows = await self._cypher(conn, f"""
                    MATCH (start {{name: '{name_esc}'}})-[*0..{int(depth)}]-(n)
                    RETURN DISTINCT n
                    LIMIT {int(limit)}
                """, cols="n agtype")

                if not node_rows:
                    return {"entities": [], "relationships": []}

                entities = []
                name_set = set()
                for row in node_rows:
                    v = row["n"]
                    props = v.get("properties", {})
                    name = props.get("name", "")
                    name_set.add(name)
                    entities.append({
                        "name": name,
                        "type": v.get("label", "unknown"),
                        "properties": {
                            k: v2 for k, v2 in props.items()
                            if k not in ("name", "entity_id", "created_at", "updated_at")
                        },
                    })

                # Step 2: Get all edges between those nodes
                # Build a Cypher list of names for filtering
                name_list = ", ".join(
                    f"'{self._escape(n)}'" for n in name_set
                )
                rel_rows = await self._cypher(conn, f"""
                    MATCH (a)-[r]->(b)
                    WHERE a.name IN [{name_list}] AND b.name IN [{name_list}]
                    RETURN a, r, b
                """, cols="a agtype, r agtype, b agtype")

                relationships = []
                for row in rel_rows:
                    a_props = row["a"].get("properties", {})
                    b_props = row["b"].get("properties", {})
                    edge = row["r"]
                    relationships.append({
                        "source": a_props.get("name", ""),
                        "relation": edge.get("label", ""),
                        "target": b_props.get("name", ""),
                        "properties": dict(edge.get("properties", {})),
                    })

                return {"entities": entities, "relationships": relationships}
        except Exception:
            return {"entities": [], "relationships": []}

    # ------------------------------------------------------------------
    # Raw Cypher execution (for the agent)
    # ------------------------------------------------------------------

    async def execute_cypher(self, query: str) -> list[dict[str, Any]]:
        """Execute a raw Cypher query written by the agent.

        Infers the output column schema from the RETURN clause.
        Returns parsed results as a list of dicts.
        """
        if not self._available:
            return [{"error": "Graph store not available"}]
        try:
            cols = self._infer_return_cols(query)
            async with self._pool.acquire() as conn:
                return await self._cypher(conn, query, cols=cols)
        except Exception as e:
            return [{"error": str(e)}]

    @staticmethod
    def _infer_return_cols(query: str) -> str:
        """Infer the AS clause column spec from a Cypher RETURN clause."""
        match = re.search(
            r'\bRETURN\b\s+(.+?)(?:\s+ORDER\s+|\s+LIMIT\s+|\s+SKIP\s+|$)',
            query, re.IGNORECASE | re.DOTALL,
        )
        if not match:
            # Mutation without RETURN — use a dummy column
            return "v agtype"

        return_expr = match.group(1).strip()
        # Split on commas (crude but handles most practical Cypher)
        items = [x.strip() for x in return_expr.split(",")]
        cols = []
        for i, item in enumerate(items):
            # Check for explicit AS alias
            as_match = re.search(r'\bAS\s+(\w+)\s*$', item, re.IGNORECASE)
            if as_match:
                cols.append(f"{as_match.group(1)} agtype")
            else:
                cols.append(f"c{i} agtype")
        return ", ".join(cols)
