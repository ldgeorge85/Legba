"""ORIENT phase — context gathering from all memory layers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..cycle import AgentCycle


class OrientMixin:
    """Phase 2: Gather context from all memory layers."""

    async def _orient(self: AgentCycle) -> None:
        """
        Phase 2: Gather context.

        - Retrieve relevant memories
        - Load active goals
        - Build working context

        Query uses active goal focus (not just static seed goal) for better
        memory retrieval relevance.
        """
        self.logger.log_phase("orient")
        self.state.phase = "orient"

        # Build query from seed goal + active goal focus for better retrieval
        query_text = self.state.seed_goal[:300]

        # Load active goals first so we can use them in the query
        self._active_goals = await self.goals.get_active_goals()

        # Enrich query with the highest-priority active goal
        if self._active_goals:
            top_goal = self._active_goals[0]
            goal_desc = top_goal.description if hasattr(top_goal, 'description') else str(top_goal)
            query_text += f" Current focus: {str(goal_desc)[:200]}"

        try:
            query_embedding = await self.llm.generate_embedding(query_text)
        except Exception:
            query_embedding = None

        # Retrieve context from all memory layers
        self._memory_context = await self.memory.retrieve_context(
            query_embedding=query_embedding,
            limit=self.config.agent.memory_retrieval_limit,
            current_cycle=self.state.cycle_number,
        )

        # Get NATS queue summary for context
        self._queue_summary = await self.nats.queue_summary()

        # Real-time infrastructure health check (overrides stale journal narratives)
        self._infra_health = ""
        try:
            health_lines = ["## Infrastructure Health (live check, this cycle)"]
            # Postgres
            if self.memory.structured and self.memory.structured._available:
                sig_count = await self.memory.structured._pool.fetchval("SELECT count(*) FROM signals")
                health_lines.append(f"- PostgreSQL: **HEALTHY** ({sig_count} signals in store)")
            else:
                health_lines.append("- PostgreSQL: UNAVAILABLE")
            # Graph
            if self.memory.graph and self.memory.graph.available:
                health_lines.append("- Graph (AGE): **HEALTHY**")
            else:
                health_lines.append("- Graph (AGE): UNAVAILABLE")
            # Redis
            try:
                await self.memory.registers._redis.ping()
                health_lines.append("- Redis: **HEALTHY**")
            except Exception:
                health_lines.append("- Redis: UNAVAILABLE")
            # Qdrant
            if self.memory.episodic and self.memory.episodic._available:
                health_lines.append("- Qdrant: **HEALTHY**")
            else:
                health_lines.append("- Qdrant: UNAVAILABLE")

            health_lines.append("")
            health_lines.append("If your journal says a service is down but this check says HEALTHY, trust this check. Your journal may carry stale observations from previous cycles.")
            self._infra_health = "\n".join(health_lines)
        except Exception:
            pass

        # Build knowledge graph summary (prefixed with live health status)
        self._graph_inventory = self._infra_health + "\n\n" if self._infra_health else ""
        try:
            if self.memory.graph and self.memory.graph.available:
                lines = ["## Knowledge Graph Summary", ""]

                # Query 1: Entity type counts
                type_counts = await self.memory.graph.execute_cypher(
                    "MATCH (n) "
                    "RETURN label(n) AS lbl, count(n) AS cnt "
                    "ORDER BY cnt DESC"
                )
                if type_counts:
                    parts = []
                    total = 0
                    for row in type_counts:
                        lbl = str(row.get("lbl", "?")).strip('"')
                        cnt = row.get("cnt", 0)
                        total += cnt
                        parts.append(f"{lbl} ({cnt})")
                    lines.append(f"**Graph entities ({total}):** {', '.join(parts)}")
                    lines.append("")

                # Query 2: Relationship count
                rel_counts = await self.memory.graph.execute_cypher(
                    "MATCH ()-[r]->() "
                    "RETURN type(r) AS rtype, count(r) AS cnt "
                    "ORDER BY cnt DESC LIMIT 15"
                )
                if rel_counts:
                    parts = []
                    for row in rel_counts:
                        rtype = str(row.get("rtype", "?")).strip('"')
                        cnt = row.get("cnt", 0)
                        parts.append(f"{rtype} ({cnt})")
                    lines.append(f"**Top relationships:** {', '.join(parts)}")
                    lines.append("")

                # Query 3: Unknown entities warning
                unknowns = await self.memory.graph.execute_cypher(
                    "MATCH (n:Unknown) RETURN n.name AS name LIMIT 20"
                )
                if unknowns:
                    unames = [str(r.get("name", "?")).strip('"') for r in unknowns]
                    lines.append(
                        f"**Warning: {len(unames)} Unknown-type entities "
                        f"need classification:** {', '.join(unames[:10])}"
                        + ("..." if len(unames) > 10 else "")
                    )
                    lines.append("")

                if not type_counts:
                    lines.append("Graph is empty. Build it by storing entities and relationships from events and source documents.")
                    lines.append("")

                self._graph_inventory = "\n".join(lines)
        except Exception as e:
            self.logger.log_error(f"Graph inventory query failed: {e}")

        # Source health stats (injected into graph inventory for planning)
        self._source_health = ""
        try:
            if self.memory and self.memory.structured and self.memory.structured._available:
                import asyncpg
                from ...shared.config import PostgresConfig
                pg = PostgresConfig.from_env()
                conn = await asyncpg.connect(
                    host=pg.host, port=pg.port, user=pg.user,
                    password=pg.password, database=pg.database,
                )
                total_sources = await conn.fetchval("SELECT COUNT(*) FROM sources")
                sources_with_signals = await conn.fetchval(
                    "SELECT COUNT(DISTINCT source_id) FROM signals WHERE source_id IS NOT NULL"
                )
                total_signals = await conn.fetchval("SELECT COUNT(*) FROM signals")
                await conn.close()

                utilization = (sources_with_signals / total_sources * 100) if total_sources else 0
                health_line = (
                    f"\n## Source Health\n"
                    f"**{total_sources} sources registered**, "
                    f"**{sources_with_signals} have produced signals** "
                    f"({utilization:.0f}% utilization), "
                    f"**{total_signals} total signals**"
                )
                if utilization < 50:
                    health_line += (
                        f"\n**WARNING: Source utilization is very low.** "
                        f"Focus on parsing existing sources rather than adding new ones."
                    )
                self._source_health = health_line

                # Per-source quality breakdown
                try:
                    quality = await self.memory.structured.get_source_quality_summary(limit=5)
                    if quality.get("top"):
                        top_lines = ", ".join(
                            f"{s['name']} ({s['score']:.0%})" for s in quality["top"]
                        )
                        health_line += f"\n**Top quality sources:** {top_lines}"
                    if quality.get("bottom"):
                        bottom_lines = ", ".join(
                            f"{s['name']} ({s['score']:.0%})" for s in quality["bottom"]
                        )
                        health_line += f"\n**Lowest quality sources:** {bottom_lines}"
                    self._source_health = health_line
                except Exception:
                    pass

                # Append to graph inventory so the planner sees it
                if self._graph_inventory:
                    self._graph_inventory += "\n" + self._source_health
                else:
                    self._graph_inventory = self._source_health
        except Exception:
            pass

        # Ingestion gap tracking — warn if no signals stored recently
        self._ingestion_gap_warning = ""
        try:
            if self.memory and self.memory.registers:
                last_ingest_str = await self.memory.registers.get("last_ingestion_cycle")
                if last_ingest_str:
                    last_ingest = int(last_ingest_str)
                    gap = self.state.cycle_number - last_ingest
                    if gap > 5:
                        self._ingestion_gap_warning = (
                            f"\n## ⚠ Ingestion Gap Warning\n"
                            f"**No new signals have been stored for {gap} cycles** "
                            f"(last ingestion: cycle {last_ingest}). "
                            f"Data is getting stale. Prioritize fetching sources "
                            f"and storing signals this cycle."
                        )
                        if self._graph_inventory:
                            self._graph_inventory += self._ingestion_gap_warning
                        else:
                            self._graph_inventory = self._ingestion_gap_warning
        except Exception:
            pass

        # Load journal leads — investigation hints from narrate phase
        self._journal_leads = ""
        try:
            if self.memory and self.memory.registers:
                leads = await self.memory.registers.get_json("journal_leads")
                if leads and isinstance(leads, list) and leads:
                    self._journal_leads = (
                        "\n## Investigation Leads (from journal)\n"
                        + "\n".join(f"- {lead}" for lead in leads[:5])
                    )
                    if self._graph_inventory:
                        self._graph_inventory += self._journal_leads
                    else:
                        self._graph_inventory = self._journal_leads
        except Exception:
            pass

        # --- Ingestion service status (when running) ---
        self._ingestion_status = ""
        self._ingestion_heartbeat_detected = False
        self._ingestion_briefing = ""
        try:
            if self.memory and self.memory.registers:
                redis = self.memory.registers._redis
                heartbeat = await redis.get("legba:ingest:heartbeat")
                if heartbeat:
                    self._ingestion_heartbeat_detected = True
                    import time as _time
                    _now = _time.time()
                    events_1h = await redis.zcount("legba:ingest:events_1h", _now - 3600, _now)
                    events_24h = await redis.zcount("legba:ingest:events_24h", _now - 86400, _now)
                    errors_1h = await redis.zcount("legba:ingest:errors_1h", _now - 3600, _now)

                    # Count active sources
                    active_sources = 0
                    total_sources = 0
                    try:
                        import asyncpg
                        from ...shared.config import PostgresConfig
                        pg = PostgresConfig.from_env()
                        conn = await asyncpg.connect(
                            host=pg.host, port=pg.port, user=pg.user,
                            password=pg.password, database=pg.database,
                        )
                        total_sources = await conn.fetchval("SELECT COUNT(*) FROM sources")
                        active_sources = await conn.fetchval(
                            "SELECT COUNT(*) FROM sources WHERE status = 'active'"
                        )
                        # Top categories from recent signals
                        cat_rows = await conn.fetch("""
                            SELECT category, COUNT(*) as cnt
                            FROM signals
                            WHERE created_at > NOW() - INTERVAL '1 hour'
                            GROUP BY category
                            ORDER BY cnt DESC
                            LIMIT 5
                        """)
                        cat_summary = ", ".join(
                            f"{r['category']}({r['cnt']})" for r in cat_rows
                        ) if cat_rows else "none yet"

                        # High-confidence signal briefing (3.3)
                        briefing_rows = await conn.fetch("""
                            SELECT title, category, summary,
                                   array_to_string(actors, ', ') as actors_str,
                                   array_to_string(locations, ', ') as locs_str,
                                   confidence
                            FROM signals
                            WHERE created_at > NOW() - INTERVAL '2 hours'
                              AND confidence >= 0.7
                            ORDER BY confidence DESC, created_at DESC
                            LIMIT 10
                        """)
                        await conn.close()

                        if briefing_rows:
                            brief_lines = ["## High-Confidence Signal Briefing (last 2h)", ""]
                            for r in briefing_rows:
                                line = f"- **{r['title'][:100]}** [{r['category']}]"
                                if r['locs_str']:
                                    line += f" — {r['locs_str'][:60]}"
                                if r['actors_str']:
                                    line += f" (actors: {r['actors_str'][:60]})"
                                brief_lines.append(line)
                            self._ingestion_briefing = "\n".join(brief_lines)
                    except Exception:
                        cat_summary = "unavailable"

                    self._ingestion_status = (
                        f"\n## Ingestion Service Status\n"
                        f"**Service: RUNNING** (heartbeat: {heartbeat})\n"
                        f"Signals stored (1h): **{events_1h}** | "
                        f"Signals stored (24h): **{events_24h}** | "
                        f"Errors (1h): {errors_1h}\n"
                        f"Sources: {active_sources}/{total_sources} active\n"
                        f"Top categories (1h): {cat_summary}"
                    )

                    # Append to graph inventory for planner visibility
                    if self._graph_inventory:
                        self._graph_inventory += "\n" + self._ingestion_status
                    else:
                        self._graph_inventory = self._ingestion_status

                    if self._ingestion_briefing:
                        if self._graph_inventory:
                            self._graph_inventory += "\n\n" + self._ingestion_briefing
                        else:
                            self._graph_inventory = self._ingestion_briefing
        except Exception:
            pass

        # Retrieve previous cycle's reflection-forward data
        self._reflection_forward = ""
        try:
            rf = await self.memory.registers.get_json("reflection_forward")
            if rf:
                parts = []
                if rf.get("self_assessment"):
                    parts.append(f"**Self-assessment:** {rf['self_assessment']}")
                if rf.get("next_cycle_suggestion"):
                    parts.append(f"**Suggested next action:** {rf['next_cycle_suggestion']}")
                if rf.get("recent_work_pattern"):
                    parts.append(f"**Recent work pattern:** {rf['recent_work_pattern']}")
                if rf.get("stale_goal_count", 0) > 0:
                    parts.append(f"**Stale goals (no progress >1hr):** {rf['stale_goal_count']}")
                if parts:
                    self._reflection_forward = "## Previous Cycle Reflection\n" + "\n".join(parts)
        except Exception:
            pass

        # Retrieve per-goal work tracker (Phase O)
        self._goal_work_tracker: dict[str, dict] = {}
        try:
            tracker = await self.memory.registers.get_json("goal_work_tracker")
            if isinstance(tracker, dict):
                self._goal_work_tracker = tracker
        except Exception:
            pass

        # Retrieve journal context — latest consolidation + recent entries
        self._journal_context = ""
        self._journal_consolidation_text = ""
        try:
            if self.memory and self.memory.registers:
                journal_data = await self.memory.registers.get_json("journal")
                if journal_data:
                    consolidation = journal_data.get("consolidation", "")
                    entries = journal_data.get("entries", [])
                    self._journal_consolidation_text = consolidation

                    parts = []
                    if consolidation:
                        parts.append(f"### Last Consolidation\n{consolidation}")
                    if entries:
                        parts.append("### Recent Entries")
                        for e in entries[-10:]:  # Last 10 entries
                            cycle_n = e.get("cycle", "?")
                            for line in e.get("entries", []):
                                parts.append(f"- [cycle {cycle_n}] {line}")
                    self._journal_context = "\n".join(parts) if parts else ""
        except Exception:
            pass

        self.logger.log("orient_complete",
                        episodes=len(self._memory_context.get("episodes", [])),
                        goals=len(self._active_goals),
                        facts=len(self._memory_context.get("facts", [])),
                        graph_entities=len(self._graph_inventory) > 0,
                        nats_data_streams=len(self._queue_summary.data_streams),
                        has_journal=bool(self._journal_context))
