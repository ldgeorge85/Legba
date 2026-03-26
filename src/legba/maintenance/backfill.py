"""One-time and periodic backfill tasks for the maintenance daemon.

Backfills existing data into new structures (event graph vertices,
signal confidence components, etc.) as a validation exercise for
newly deployed features.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger("legba.maintenance.backfill")

GRAPH_NAME = "legba_graph"


class BackfillManager:
    """Runs backfill tasks to populate new data structures from existing data."""

    def __init__(self, pg_pool):
        self.pool = pg_pool

    async def backfill_event_graph_vertices(self) -> int:
        """Create Event vertices in AGE for all events that don't have one yet.

        Also creates INVOLVED_IN edges from event actors to the event vertex.
        Returns count of events backfilled.
        """
        from ..shared.graph_events import upsert_event_vertex, link_entity_to_event

        # Get all events
        events = await self.pool.fetch("""
            SELECT id, title, category,
                   COALESCE(lifecycle_status, 'emerging') as lifecycle_status,
                   data
            FROM events
            ORDER BY created_at
        """)

        if not events:
            return 0

        # Check which events already have vertices (by title match)
        existing = set()
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("LOAD 'age'")
                await conn.execute("SET search_path = ag_catalog, public")
                rows = await conn.fetch("""
                    SELECT * FROM cypher('legba_graph', $$
                        MATCH (e:Event) RETURN e.name
                    $$) AS (name agtype)
                """)
                for r in rows:
                    name = str(r['name']).strip('"')
                    existing.add(name)
        except Exception as e:
            logger.warning("Could not query existing event vertices: %s", e)

        backfilled = 0
        for event in events:
            title = event['title']
            if title in existing:
                continue

            try:
                await upsert_event_vertex(
                    self.pool, GRAPH_NAME,
                    event_id=str(event['id']),
                    event_title=title,
                    category=event['category'] or 'other',
                    lifecycle_status=event['lifecycle_status'],
                )

                # Link actors from event data
                data = event['data']
                if isinstance(data, str):
                    try:
                        data = json.loads(data)
                    except (json.JSONDecodeError, TypeError):
                        data = {}

                actors = []
                if isinstance(data, dict):
                    actors = data.get('actors', [])
                    if isinstance(actors, str):
                        actors = [a.strip() for a in actors.split(',') if a.strip()]

                for actor in actors[:10]:
                    try:
                        await link_entity_to_event(
                            self.pool, GRAPH_NAME,
                            entity_name=actor,
                            event_title=title,
                            role='actor',
                            confidence=0.6,
                        )
                    except Exception:
                        pass

                backfilled += 1
            except Exception as e:
                logger.debug("Failed to backfill event %s: %s", title[:50], e)

        logger.info("Event graph backfill: %d events added (%d already existed)",
                     backfilled, len(existing))
        return backfilled

    async def backfill_entity_vertices(self) -> int:
        """Create Entity vertices in AGE for all entity_profiles that don't have a graph node yet.

        This ensures the fact-to-graph materializer can find entities to link.
        Returns count of entities backfilled.
        """
        # Get existing graph entity names
        try:
            existing_rows = await self.pool.fetch(f"""
                LOAD 'age'; SET search_path = ag_catalog, public;
                SELECT * FROM cypher('{GRAPH_NAME}', $$
                    MATCH (n:Entity) RETURN n.name
                $$) AS (name agtype);
            """)
            existing_names = {str(r["name"]).strip('"') for r in existing_rows}
        except Exception:
            existing_names = set()

        # Get all entity profiles
        profiles = await self.pool.fetch(
            "SELECT canonical_name, entity_type FROM entity_profiles"
        )

        created = 0
        for p in profiles:
            name = p["canonical_name"]
            if name in existing_names:
                continue
            etype = p["entity_type"] or "Unknown"
            try:
                safe_name = name.replace('"', '\\"').replace("'", "\\'")
                safe_type = etype.replace('"', '\\"')
                await self.pool.execute(f"""
                    LOAD 'age'; SET search_path = ag_catalog, public;
                    SELECT * FROM cypher('{GRAPH_NAME}', $$
                        CREATE (n:Entity {{name: "{safe_name}", entity_type: "{safe_type}"}})
                    $$) AS (v agtype);
                """)
                created += 1
            except Exception:
                pass  # Skip duplicates or encoding issues

        logger.info("Entity vertex backfill: %d entities added (%d already existed)",
                     created, len(existing_names))
        return created

    async def backfill_situation_graph(self) -> int:
        """Create TRACKED_BY edges from events to situations in the graph.

        Uses situation_events junction table to create edges.
        """
        from ..shared.graph_events import link_event_situation

        rows = await self.pool.fetch("""
            SELECT e.title as event_title, s.name as situation_name, se.relevance
            FROM situation_events se
            JOIN events e ON e.id = se.event_id
            JOIN situations s ON s.id = se.situation_id
        """)

        linked = 0
        for row in rows:
            try:
                await link_event_situation(
                    self.pool, GRAPH_NAME,
                    event_title=row['event_title'],
                    situation_name=row['situation_name'],
                    relevance=row['relevance'] or 1.0,
                )
                linked += 1
            except Exception:
                pass

        logger.info("Situation graph backfill: %d TRACKED_BY edges created", linked)
        return linked

    async def backfill_edge_properties(self) -> int:
        """Set default temporal properties on graph edges that predate Phase 4.5.1.

        Edges from the seed CSV import lack weight, confidence, evidence_count,
        last_evidenced, and volatility.  This sets sensible defaults on any edge
        where evidence_count IS NULL, making the backfill idempotent.

        Returns the number of edges updated.
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        try:
            async with self.pool.acquire() as conn:
                await conn.execute("LOAD 'age'")
                await conn.execute("SET search_path = ag_catalog, public")

                rows = await conn.fetch(f"""
                    SELECT * FROM cypher('{GRAPH_NAME}', $$
                        MATCH ()-[r]->()
                        WHERE r.evidence_count IS NULL
                        SET r.weight = 0.5,
                            r.confidence = 0.5,
                            r.evidence_count = 1,
                            r.last_evidenced = '{today}',
                            r.volatility = 0.0
                        RETURN count(r)
                    $$) AS (cnt agtype)
                """)

                updated = 0
                if rows:
                    raw = rows[0]["cnt"]
                    # AGE returns agtype — strip quotes if stringified
                    updated = int(str(raw).strip('"'))

                logger.info(
                    "Edge property backfill: %d edges updated with default temporal properties",
                    updated,
                )
                return updated

        except Exception as e:
            logger.warning("Edge property backfill failed: %s", e)
            return 0

    # Predicates that map directly to graph edge types
    GRAPH_PREDICATES = {
        "HostileTo", "AlliedWith", "MemberOf", "LeaderOf", "OperatesIn",
        "LocatedIn", "BordersWith", "TradesWith", "SanctionedBy",
        "SuppliesWeaponsTo", "AffiliatedWith", "PartOf", "FundedBy",
        "AtWarWith", "ConductsMilitaryOperationsIn", "SponsorOf",
    }

    async def backfill_graph_from_facts(self) -> int:
        """Create graph edges from structured facts.

        Reads facts with graph-relevant predicates and creates corresponding
        AGE edges if they don't already exist. Carries valid_from/valid_until
        as since/until edge properties.
        """
        facts = await self.pool.fetch("""
            SELECT subject, predicate, value, confidence, valid_from, valid_until
            FROM facts
            WHERE superseded_by IS NULL
              AND predicate = ANY($1)
        """, list(self.GRAPH_PREDICATES))

        created = 0
        now_iso = datetime.now(timezone.utc).isoformat()

        # Get existing edges to avoid duplicates (AGE doesn't support MERGE ON CREATE SET)
        existing_edges = set()
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("LOAD 'age'")
                await conn.execute("SET search_path = ag_catalog, public")
                rows = await conn.fetch(f"""
                    SELECT * FROM cypher('{GRAPH_NAME}', $$
                        MATCH (a:Entity)-[r]->(b:Entity)
                        RETURN a.name, type(r), b.name
                    $$) AS (a agtype, r agtype, b agtype)
                """)
                for r in rows:
                    a = str(r['a']).strip('"')
                    rel = str(r['r']).strip('"')
                    b = str(r['b']).strip('"')
                    existing_edges.add((a, rel, b))
        except Exception:
            pass  # If we can't check, we'll create and let dupes happen

        for fact in facts:
            # Skip if edge already exists
            if (fact['subject'], fact['predicate'], fact['value']) in existing_edges:
                continue
            try:
                subj = fact['subject'].replace('"', '\\"')
                val = fact['value'].replace('"', '\\"')
                pred = fact['predicate']
                conf = fact['confidence'] or 0.5
                since = fact['valid_from'].isoformat() if fact['valid_from'] else None
                until = fact['valid_until'].isoformat() if fact['valid_until'] else None

                # Build optional temporal SET clauses
                extra_set = ""
                if since:
                    extra_set += f', r.since = "{since}"'
                if until:
                    extra_set += f', r.until = "{until}"'

                # AGE doesn't support MERGE ON CREATE SET — use CREATE directly
                # Duplicates are handled by the try/except (AGE may error on duplicate edges)
                props = (
                    f'{{weight: {conf}, confidence: {conf}, '
                    f'evidence_count: 1, last_evidenced: "{now_iso}", '
                    f'volatility: 0.0'
                )
                if since:
                    props += f', since: "{since}"'
                if until:
                    props += f', until: "{until}"'
                props += '}'

                cypher = (
                    f'MATCH (a:Entity {{name: "{subj}"}}), '
                    f'(b:Entity {{name: "{val}"}}) '
                    f'CREATE (a)-[r:{pred} {props}]->(b) '
                    f'RETURN r'
                )

                async with self.pool.acquire() as conn:
                    await conn.execute("LOAD 'age'")
                    await conn.execute("SET search_path = ag_catalog, public")
                    await conn.execute(
                        f"SELECT * FROM cypher('{GRAPH_NAME}', $${cypher}$$) AS (r agtype)"
                    )
                created += 1
            except Exception:
                pass  # Skip edges where entities don't exist as graph nodes

        logger.info("Fact-to-graph backfill: %d edges created from %d eligible facts",
                     created, len(facts))
        return created
