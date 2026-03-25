"""ORIENT phase — context gathering from all memory layers."""

from __future__ import annotations

from collections import Counter
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from ..cycle import AgentCycle


def _format_differential_briefing(diff: dict[str, Any]) -> str:
    """Format the raw differential JSON into a concise human-readable briefing.

    Produces a "Since Your Last Cycle" section that summarises changes without
    dumping raw data.  Returns empty string if nothing notable happened.
    """
    summary = diff.get("summary", {})
    lines: list[str] = []

    # --- New signals ---
    sig_count = summary.get("new_signal_count", 0)
    if sig_count:
        # Group by situation name for a quick topic breakdown
        signals = diff.get("new_signals", [])
        sit_counter: Counter = Counter()
        for s in signals:
            sit_counter[s.get("situation_name", "unlinked")] += 1
        top_topics = sit_counter.most_common(5)
        topic_parts = ", ".join(f"{cnt} {name}" for name, cnt in top_topics)
        lines.append(f"- {sig_count} new signals ({topic_parts})")

    # --- Event transitions ---
    evt_count = summary.get("event_transition_count", 0)
    if evt_count:
        events = diff.get("event_transitions", [])
        evt_details = []
        for e in events[:5]:
            title = (e.get("title") or "untitled")[:60]
            evt_details.append(f'"{title}"')
        lines.append(
            f"- {evt_count} event state change{'s' if evt_count != 1 else ''}: "
            + ", ".join(evt_details)
        )

    # --- Watchlist matches ---
    wl_count = summary.get("watchlist_match_count", 0)
    if wl_count:
        matches = diff.get("watchlist_matches", [])
        # Group by watch name
        watch_counter: Counter = Counter()
        for m in matches:
            watch_counter[m.get("watch_name", "unknown")] += 1
        wl_parts = ", ".join(
            f'"{name}" matched {cnt}x' for name, cnt in watch_counter.most_common(3)
        )
        lines.append(f"- {wl_count} watchlist trigger{'s' if wl_count != 1 else ''}: {wl_parts}")

    # --- Entity anomalies ---
    ent_count = summary.get("entity_anomaly_count", 0)
    if ent_count:
        entities = diff.get("entity_anomalies", [])
        ent_details = []
        for ea in entities[:3]:
            name = (ea.get("canonical_name") or "unknown")[:40]
            recent = ea.get("recent_link_count", 0)
            ent_details.append(f"{name} ({recent} recent links)")
        lines.append(
            f"- {ent_count} entity anomal{'ies' if ent_count != 1 else 'y'}: "
            + ", ".join(ent_details)
        )

    # --- Fact changes ---
    fact_count = summary.get("fact_change_count", 0)
    if fact_count:
        facts = diff.get("fact_changes", [])
        created = sum(1 for f in facts if f.get("change_type") == "created")
        superseded = sum(1 for f in facts if f.get("change_type") == "superseded")
        updated = fact_count - created - superseded
        parts = []
        if created:
            parts.append(f"{created} new")
        if updated:
            parts.append(f"{updated} updated")
        if superseded:
            parts.append(f"{superseded} superseded")
        lines.append(f"- {fact_count} fact change{'s' if fact_count != 1 else ''} ({', '.join(parts)})")

    # --- Hypothesis changes ---
    hyp_count = summary.get("hypothesis_change_count", 0)
    if hyp_count:
        hyps = diff.get("hypothesis_changes", [])
        hyp_details = []
        for h in hyps[:3]:
            thesis = (h.get("thesis") or "?")[:50]
            balance = h.get("evidence_balance", 0)
            hyp_details.append(f'"{thesis}" (balance {balance:+d})')
        lines.append(
            f"- {hyp_count} hypothesis update{'s' if hyp_count != 1 else ''}: "
            + ", ".join(hyp_details)
        )

    if not lines:
        return ""

    return "## Since Your Last Cycle\n" + "\n".join(lines)


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
                async with self.memory.structured._pool.acquire() as conn:
                    active_sources = await conn.fetchval(
                        "SELECT COUNT(*) FROM sources WHERE status = 'active'"
                    )
                    sources_fetched_recently = await conn.fetchval(
                        "SELECT COUNT(*) FROM sources WHERE status = 'active' "
                        "AND last_successful_fetch_at > NOW() - INTERVAL '6 hours'"
                    )
                    total_signals = await conn.fetchval("SELECT COUNT(*) FROM signals")
                    signals_last_hour = await conn.fetchval(
                        "SELECT COUNT(*) FROM signals WHERE created_at > NOW() - INTERVAL '1 hour'"
                    )

                fetch_rate = (sources_fetched_recently / active_sources * 100) if active_sources else 0
                health_line = (
                    f"\n## Source Health\n"
                    f"**{active_sources} active sources**, "
                    f"**{sources_fetched_recently} fetched in last 6h** "
                    f"({fetch_rate:.0f}% fetch rate), "
                    f"**{total_signals} total signals** "
                    f"({signals_last_hour} in last hour)"
                )
                if fetch_rate < 50:
                    health_line += (
                        f"\n**NOTE: Some sources may be slow or failing.** "
                        f"This is normal — not all sources update frequently."
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
                        async with self.memory.structured._pool.acquire() as conn:
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

        # Compute state variables for dynamic cycle type selection
        self._uncurated_count = 0
        self._stale_entity_count = 0
        self._last_analysis_cycle = 0
        try:
            if self.memory.structured and self.memory.structured._available:
                async with self.memory.structured._pool.acquire() as conn:
                    # Recent uncurated signals: last 24h, no event link, non-junk.
                    # Temporal window prevents the all-time backlog from permanently
                    # monopolizing CURATE cycles. Uses event links (not entity links)
                    # because event curation is what the agent can actually affect.
                    self._uncurated_count = await conn.fetchval("""
                        SELECT count(*) FROM signals s
                        LEFT JOIN signal_event_links sel ON sel.signal_id = s.id
                        WHERE sel.signal_id IS NULL
                          AND s.category != 'other'
                          AND s.created_at > NOW() - INTERVAL '24 hours'
                    """) or 0

                    # Stale entities (below 30% completeness)
                    self._stale_entity_count = await conn.fetchval("""
                        SELECT count(*) FROM entity_profiles
                        WHERE completeness_score < 0.3
                    """) or 0

            # Last analysis cycle from Redis
            if self.memory.registers:
                import json as _json
                snapshot = await self.memory.registers.get_json("analysis_snapshot")
                if snapshot and isinstance(snapshot, dict):
                    self._last_analysis_cycle = snapshot.get("cycle", 0)
        except Exception:
            pass

        # --- Event lifecycle distribution ---
        self._lifecycle_distribution = ""
        try:
            if self.memory.structured and self.memory.structured._available:
                async with self.memory.structured._pool.acquire() as conn:
                    try:
                        lifecycle_rows = await conn.fetch("""
                            SELECT lifecycle_status, count(*) AS cnt
                            FROM events
                            GROUP BY lifecycle_status
                            ORDER BY cnt DESC
                        """)
                        if lifecycle_rows:
                            parts = [f"{r['lifecycle_status']}({r['cnt']})" for r in lifecycle_rows]
                            self._lifecycle_distribution = (
                                f"\n## Event Lifecycle Distribution\n"
                                f"**{', '.join(parts)}**"
                            )
                            if self._graph_inventory:
                                self._graph_inventory += self._lifecycle_distribution
                            else:
                                self._graph_inventory = self._lifecycle_distribution
                    except Exception:
                        pass  # Column may not exist yet
        except Exception:
            pass

        # --- Subconscious differential (from background processing) ---
        # Read the structured differential, format a concise briefing for the
        # agent, and clear it so it doesn't repeat next cycle.
        self._differential_briefing = ""
        try:
            if self.memory and self.memory.registers:
                redis = self.memory.registers._redis
                diff_raw = await redis.get("legba:subconscious:differential")
                if diff_raw:
                    import json as _json
                    diff_text = diff_raw if isinstance(diff_raw, str) else diff_raw.decode("utf-8")
                    diff = _json.loads(diff_text)
                    briefing = _format_differential_briefing(diff)
                    if briefing:
                        self._differential_briefing = briefing
                        # Push to assembler so all prompt methods can inject it
                        if self.assembler:
                            self.assembler._differential_briefing = briefing
                    # Mark as consumed so the next cycle gets a fresh diff
                    await redis.delete("legba:subconscious:differential")
        except Exception:
            pass

        # --- Cache task backlog counts for Tier 3 scoring ---
        # _select_cycle_type() is sync, so we pre-fetch counts here.
        self._backlog_survey_count = 0
        self._backlog_research_count = 0
        try:
            _redis = self.memory.registers._redis if self.memory and self.memory.registers else None
            if _redis:
                from ...shared.task_backlog import TaskBacklog
                _backlog = TaskBacklog(_redis)
                self._backlog_survey_count = await _backlog.task_count(cycle_type="SURVEY")
                self._backlog_research_count = await _backlog.task_count(cycle_type="RESEARCH")
        except Exception:
            pass

        # --- Priority stack: ranked situation advisory ---
        self._priority_context = ""
        try:
            if (self.memory.structured and self.memory.structured._available
                    and self.memory and self.memory.registers):
                from ...shared.priority import compute_priority_stack, format_priority_stack
                from ..prompt import templates as _prio_tpl

                stack = await compute_priority_stack(
                    pool=self.memory.structured._pool,
                    redis_client=self.memory.registers._redis,
                    top_n=7,
                )
                if stack:
                    formatted = format_priority_stack(stack)
                    self._priority_context = _prio_tpl.PRIORITY_STACK_TEMPLATE.format(
                        priority_items=formatted,
                    )
        except Exception as e:
            self.logger.log_error(f"Priority stack computation failed (non-fatal): {e}")

        self.logger.log("orient_complete",
                        episodes=len(self._memory_context.get("episodes", [])),
                        goals=len(self._active_goals),
                        facts=len(self._memory_context.get("facts", [])),
                        graph_entities=len(self._graph_inventory) > 0,
                        nats_data_streams=len(self._queue_summary.data_streams),
                        has_journal=bool(self._journal_context),
                        has_differential=bool(self._differential_briefing),
                        uncurated=self._uncurated_count,
                        stale_entities=self._stale_entity_count,
                        last_analysis=self._last_analysis_cycle,
                        has_priority_stack=bool(self._priority_context))
