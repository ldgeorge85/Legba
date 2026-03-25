"""INTROSPECTION phase — deep self-assessment, analysis reports, operator scorecard.

JDL Level 4: Self-assessment and world reporting.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import uuid4

from ...shared.schemas.comms import InboxMessage, OutboxMessage

if TYPE_CHECKING:
    from ..cycle import AgentCycle


# Tools allowed during introspection cycles (internal queries + graph building)
INTROSPECTION_TOOLS: frozenset[str] = frozenset({
    "graph_query", "graph_store", "graph_analyze",
    "memory_query", "memory_store", "memory_promote", "memory_supersede",
    "entity_inspect", "entity_profile",
    "os_search",
    "note_to_self", "explain_tool",
    "goal_update", "goal_create",
    "cycle_complete",
    # Predictions and hypotheses
    "prediction_create", "prediction_update", "prediction_list",
    "hypothesis_create", "hypothesis_evaluate", "hypothesis_list",
})


class IntrospectMixin:
    """Introspection cycle: deep review of knowledge base."""

    _REPORT_INDEX = "legba-reports"

    def _is_introspection_cycle(self: AgentCycle) -> bool:
        """Check if this cycle should be a deep introspection instead of normal ops."""
        interval = self.config.agent.mission_review_interval
        return interval > 0 and self.state.cycle_number % interval == 0

    async def _mission_review(self: AgentCycle) -> None:
        """Deep introspection cycle.

        Runs a full REASON+ACT loop but restricts tools to internal queries
        only. The agent surveys its knowledge base, finds gaps, discovers
        cross-domain connections, and strengthens the graph — then feeds
        its findings back into the next normal cycles.
        """
        self.logger.log_phase("mission_review")

        # Gather performance data
        recent_work_pattern = "unknown"
        try:
            rf = await self.memory.registers.get_json("reflection_forward")
            if rf:
                recent_work_pattern = rf.get("recent_work_pattern", "unknown")
        except Exception:
            pass

        # Load deferred goals
        deferred_goals_data = []
        try:
            deferred_goals = await self.goals.get_deferred_goals(self.state.cycle_number)
            deferred_goals_data = [g.model_dump() for g in deferred_goals]
        except Exception:
            pass

        # Build introspection prompt with tool definitions for allowed tools only
        inbox_messages = [InboxMessage(**m) for m in self.state.inbox_messages]
        review_messages = self.assembler.assemble_introspection_prompt(
            cycle_number=self.state.cycle_number,
            seed_goal=self.state.seed_goal,
            active_goals=[g.model_dump() for g in self._active_goals],
            deferred_goals=deferred_goals_data,
            recent_work_pattern=recent_work_pattern,
            allowed_tools=INTROSPECTION_TOOLS,
            inbox_messages=inbox_messages if inbox_messages else None,
        )

        # Create a filtered executor that only allows introspection tools
        async def introspection_executor(tool_name: str, arguments: dict) -> str:
            if tool_name not in INTROSPECTION_TOOLS:
                return (f"Tool '{tool_name}' is not available during introspection. "
                        f"This is an internal review cycle — only query and analysis tools are available: "
                        f"{', '.join(sorted(INTROSPECTION_TOOLS))}")
            return await self.executor.execute(tool_name, arguments)

        # Set cycle plan so reflect phase knows this was introspection
        self._cycle_plan = "INTROSPECTION CYCLE: Deep review of knowledge base, graph audit, cross-domain analysis, gap identification."

        try:
            self._final_response, self._conversation = await self.llm.reason_with_tools(
                messages=review_messages,
                tool_executor=introspection_executor,
                purpose="introspection",
                max_steps=self.config.agent.max_reasoning_steps,
                stop_check=self._make_stop_checker(),
            )

            # Count introspection actions
            self.state.actions_taken = sum(
                1 for m in self._conversation
                if m.role == "user" and m.content.startswith("[Tool Result:")
            )

            # Extract key findings from the final response and working memory
            parts = ["## Introspection Findings (cycle {})".format(self.state.cycle_number)]
            wm_summary = self.llm.working_memory.summary()
            if wm_summary:
                parts.append(wm_summary)
            elif self._final_response:
                parts.append(self._final_response[:1000])

            self._reflection_forward = "\n".join(parts) + "\n\n" + self._reflection_forward

            self.logger.log("mission_review_complete",
                            mode="introspection",
                            actions=self.state.actions_taken,
                            response_length=len(self._final_response))

        except Exception as e:
            self._final_response = f"Introspection failed: {e}"
            self._conversation = []
            self.logger.log_error(f"Introspection cycle failed: {e}")

    async def _generate_analysis_report(self: AgentCycle) -> None:
        """Generate a full Current World Assessment (introspection only).

        Queries actual data from all stores to ground the report in facts,
        preventing the LLM from hallucinating leaders, events, or programs.
        """
        self.logger.log_phase("analysis_report")
        try:
            # Gather context for the report
            graph_summary = self._graph_inventory or "(no graph data)"

            # --- Key relationships from graph ---
            key_relationships = ""
            try:
                if self.memory.graph and self.memory.graph.available:
                    rel_lines = []
                    for rel_type in ["LeaderOf", "HostileTo", "AlliedWith", "SuppliesWeaponsTo",
                                     "SanctionedBy", "MemberOf", "OperatesIn", "OccupiedBy",
                                     "SignatoryTo", "TradesWith"]:
                        rels = await self.memory.graph.execute_cypher(
                            f"MATCH (a)-[r:{rel_type}]->(b) "
                            f"RETURN a.name AS from, b.name AS to"
                        )
                        if rels:
                            for r in rels:
                                fn = str(r.get("from", "?")).strip('"')
                                tn = str(r.get("to", "?")).strip('"')
                                rel_lines.append(f"- {fn} --[{rel_type}]--> {tn}")
                    key_relationships = "\n".join(rel_lines) if rel_lines else "(no relationships found)"
            except Exception:
                key_relationships = "(could not query relationships)"

            # --- Entity profiles with summaries ---
            entity_profiles_text = ""
            entity_count = 0
            try:
                if self.memory.graph and self.memory.graph.available:
                    result = await self.memory.graph.execute_cypher(
                        "MATCH (n) RETURN count(n) AS cnt"
                    )
                    if result:
                        entity_count = result[0].get("cnt", 0)
            except Exception:
                pass

            try:
                async with self.memory.structured._pool.acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT data->>'name' AS name, data->>'type' AS type, "
                        "data->>'summary' AS summary "
                        "FROM entity_profiles "
                        "WHERE data->>'summary' IS NOT NULL AND data->>'summary' != '' "
                        "ORDER BY updated_at DESC LIMIT 60"
                    )
                if rows:
                    ep_lines = []
                    for r in rows:
                        ep_lines.append(f"- [{r['type']}] {r['name']}: {r['summary']}")
                    entity_profiles_text = "\n".join(ep_lines)
            except Exception:
                pass
            if not entity_profiles_text:
                entity_profiles_text = f"({entity_count} entities in graph, but no detailed profiles with summaries available)"

            # --- Temporal reference reports (3-point layering) ---
            # Pull 3 reference points from report history for temporal depth:
            #   last:  most recent (~3h ago) → what changed just now
            #   24h:   closest to 24h ago   → regional trajectory
            #   7d:    closest to 7d ago    → longer arc patterns
            report_history = await self.memory.registers.get_json("report_history") or []

            def _extract_section(content: str, header: str, max_chars: int = 1000) -> str:
                idx = content.find(header)
                if idx < 0:
                    return ""
                end_idx = content.find("\n## ", idx + len(header))
                section = content[idx:end_idx if end_idx > 0 else idx + max_chars].strip()
                return section[:max_chars]

            def _find_closest_report(history: list, hours_ago: int) -> dict:
                if not history:
                    return {}
                from datetime import timedelta
                target = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
                best = history[0]
                best_delta = float("inf")
                for r in history:
                    ts_str = r.get("timestamp", "")
                    if not ts_str:
                        continue
                    try:
                        ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                        delta = abs((ts - target).total_seconds())
                        if delta < best_delta:
                            best_delta = delta
                            best = r
                    except (ValueError, TypeError):
                        continue
                return best

            # Last report (most recent)
            ref_last = report_history[-1] if report_history else {}
            ref_last_content = ref_last.get("content", "")
            ref_last_cycle = ref_last.get("cycle", 0)
            ref_last_exec = _extract_section(ref_last_content, "## 2. Executive Summary", 800)
            if not ref_last_exec:
                ref_last_exec = _extract_section(ref_last_content, "## Executive Summary", 800)
            # Fallback for previous_assessment compat
            previous_assessment = ref_last_exec or ref_last_content[:1500]
            last_report_cycle = ref_last_cycle

            # 24h reference
            ref_24h = _find_closest_report(report_history, hours_ago=24)
            ref_24h_content = ref_24h.get("content", "")
            ref_24h_cycle = ref_24h.get("cycle", 0)
            ref_24h_regional = _extract_section(ref_24h_content, "## 3. Regional Situation", 1200)
            if not ref_24h_regional:
                ref_24h_regional = _extract_section(ref_24h_content, "## Regional", 1200)

            # 7d reference
            ref_7d = _find_closest_report(report_history, hours_ago=168)
            ref_7d_content = ref_7d.get("content", "")
            ref_7d_cycle = ref_7d.get("cycle", 0)
            ref_7d_patterns = _extract_section(ref_7d_content, "## 4. Emerging Patterns", 1000)
            if not ref_7d_patterns:
                ref_7d_patterns = _extract_section(ref_7d_content, "## Emerging", 1000)

            # --- Recent events grouped by entity for correlation ---
            # Query derived events (not raw signals) and group by shared entities
            # so the LLM sees related events together (e.g., Cuba earthquake + Cuba blackout)
            recent_events = ""
            try:
                if self.memory.structured and self.memory.structured._available:
                    async with self.memory.structured._pool.acquire() as conn:
                        rows = await conn.fetch(
                            "SELECT data FROM events ORDER BY time_start DESC NULLS LAST, created_at DESC LIMIT 60"
                        )

                    if rows:
                        # Parse events and extract entities for grouping
                        events_data = []
                        for r in rows:
                            d = json.loads(r["data"]) if isinstance(r["data"], str) else r["data"]
                            entities = set()
                            for a in (d.get("actors") or []):
                                if a and len(a) >= 3:
                                    entities.add(a.lower())
                            for loc in (d.get("locations") or []):
                                if loc and len(loc) >= 3:
                                    entities.add(loc.lower())
                            events_data.append({"d": d, "entities": entities})

                        # Group: events sharing 2+ non-generic entities go in the same group
                        # Skip generic terms that would over-group (nws, united states, etc.)
                        generic = {"united states", "us", "usa", "nws", "world", "global", "europe", "asia", "africa"}
                        groups: list[list[dict]] = []
                        used = set()
                        for i, ev in enumerate(events_data):
                            if i in used:
                                continue
                            group = [ev]
                            used.add(i)
                            for j in range(i + 1, len(events_data)):
                                if j in used:
                                    continue
                                overlap = (ev["entities"] & events_data[j]["entities"]) - generic
                                if len(overlap) >= 2:
                                    group.append(events_data[j])
                                    used.add(j)
                            groups.append(group)

                        # Sort groups by size (largest clusters first) then by recency
                        groups.sort(key=lambda g: (-len(g), 0))

                        # Format as entity-grouped sections
                        lines = []
                        if ref_last_cycle > 0:
                            lines.append(f"(Derived events, grouped by shared entities. Last report: cycle {ref_last_cycle})")
                        for group in groups[:30]:  # Cap at 30 groups
                            if len(group) > 1:
                                # Find common entities for header
                                common = group[0]["entities"]
                                for ev in group[1:]:
                                    common = common & ev["entities"]
                                header_entities = ", ".join(sorted(common)[:3]).title() if common else "Related"
                                lines.append(f"\n### {header_entities} ({len(group)} events)")
                            for ev in group:
                                d = ev["d"]
                                title = d.get("title", "untitled")
                                cat = d.get("category", "?")
                                sev = d.get("severity", "medium")
                                sc = d.get("signal_count", 0)
                                ts = d.get("time_start", d.get("created_at", "?"))
                                actors = d.get("actors") or []
                                locations = d.get("locations") or []
                                actor_str = f" | Actors: {', '.join(actors[:5])}" if actors else ""
                                loc_str = f" | Location: {', '.join(locations[:3])}" if locations else ""
                                line = f"- [{cat}/{sev}] {title} ({ts}) [{sc} signals]{actor_str}{loc_str}"
                                summary = d.get("summary", "")
                                if summary:
                                    line += f"\n  {summary[:200]}"
                                lines.append(line)
                        recent_events = "\n".join(lines)
            except Exception:
                pass

            if not recent_events:
                recent_events = "(no derived events available)"

            # Coverage regions from graph
            coverage_regions = ""
            try:
                if self.memory.graph and self.memory.graph.available:
                    regions = await self.memory.graph.execute_cypher(
                        "MATCH (n:Country) RETURN n.name AS name ORDER BY name"
                    )
                    if regions:
                        names = [str(r.get("name", "?")).strip('"') for r in regions]
                        coverage_regions = ", ".join(names)
            except Exception:
                pass

            # Get current narrative (for voice/continuity only)
            journal_data = await self.memory.registers.get_json(self._JOURNAL_KEY) or {}
            narrative = journal_data.get("consolidation", "")

            # Build temporal context — 3 reference points woven into narrative
            narrative_with_prev = narrative

            temporal_sections = []
            if ref_last_exec:
                hours_ago = "?"
                try:
                    ts = datetime.fromisoformat(str(ref_last.get("timestamp", "")).replace("Z", "+00:00"))
                    hours_ago = f"{(datetime.now(timezone.utc) - ts).total_seconds() / 3600:.0f}"
                except (ValueError, TypeError):
                    pass
                temporal_sections.append(
                    f"### Your Last Assessment ({hours_ago}h ago, cycle {ref_last_cycle})\n"
                    f"{ref_last_exec}"
                )

            if ref_24h_regional and ref_24h_cycle != ref_last_cycle:
                temporal_sections.append(
                    f"### Your Assessment ~24 Hours Ago (cycle {ref_24h_cycle})\n"
                    f"Regional highlights:\n{ref_24h_regional}"
                )

            if ref_7d_patterns and ref_7d_cycle not in (ref_last_cycle, ref_24h_cycle):
                temporal_sections.append(
                    f"### Your Assessment ~7 Days Ago (cycle {ref_7d_cycle})\n"
                    f"Patterns and watch items:\n{ref_7d_patterns}"
                )

            if temporal_sections:
                narrative_with_prev += (
                    "\n\n## TEMPORAL CONTEXT\n\n"
                    + "\n\n".join(temporal_sections)
                    + "\n\n"
                    "Use these reference points to provide temporal depth:\n"
                    "- Executive Summary: what changed in the last few hours (vs Last Assessment)\n"
                    "- Regional Situation: how each region's trajectory has evolved over 24h "
                    "(escalating, stable, de-escalating — cite the 24h reference)\n"
                    "- Emerging Patterns: what trends are visible over the 7-day arc "
                    "(acceleration, reversal, persistence — cite the 7d reference)\n\n"
                    "Your report MUST stand alone as a complete SA document. "
                    "The temporal references add depth, not replace content."
                )
            elif previous_assessment:
                # Fallback if no report history yet
                narrative_with_prev += (
                    f"\n\n## YOUR PREVIOUS ASSESSMENT (cycle {last_report_cycle})\n"
                    f"{previous_assessment}\n\n"
                    f"Address what has changed since this assessment."
                )

            # --- Leader / volatile-fact freshness audit ---
            # Facts with volatile predicates that haven't been updated in 200+
            # cycles are likely stale (leadership changes, regime turnover, etc.).
            # Inject a warning so the report generator knows not to trust them.
            stale_leaders: list[dict] = []
            try:
                if self.memory.structured and self.memory.structured._available:
                    async with self.memory.structured._pool.acquire() as conn:
                        rows = await conn.fetch(
                            "SELECT subject, predicate, value, source_cycle "
                            "FROM facts "
                            "WHERE predicate IN ("
                            "  'LeaderOf', 'HeadOfState', 'HeadOfGovernment', "
                            "  'President', 'PrimeMinister'"
                            ") "
                            "AND superseded_by IS NULL "
                            "AND source_cycle < $1 "
                            "ORDER BY source_cycle ASC LIMIT 20",
                            self.state.cycle_number - 200,
                        )
                        stale_leaders = [dict(r) for r in rows]
            except Exception:
                pass

            # Build set of stale entity names for confidence propagation
            stale_entity_names: set[str] = set()
            if stale_leaders:
                for sl in stale_leaders:
                    stale_entity_names.add(sl["subject"].lower())
                    stale_entity_names.add(sl["value"].lower())

                stale_text = "\n\n### STALE LEADER FACTS (verify before citing)\n"
                stale_text += (
                    "The following leader/head-of-state facts have not been "
                    "updated in 200+ cycles and may be outdated. Do NOT cite "
                    "these in the report unless you can corroborate them from "
                    "recent events or entity profiles above.\n"
                )
                for sl in stale_leaders:
                    stale_text += (
                        f"- {sl['subject']} {sl['predicate']} "
                        f"{sl['value']} (from cycle {sl['source_cycle']})\n"
                    )
                entity_profiles_text += stale_text

                # Annotate relationship lines involving stale entities
                if stale_entity_names and key_relationships:
                    annotated_lines = []
                    for line in key_relationships.split("\n"):
                        line_lower = line.lower()
                        if any(name in line_lower for name in stale_entity_names):
                            annotated_lines.append(line + "  ⚠ REDUCED CONFIDENCE — involves stale entity")
                        else:
                            annotated_lines.append(line)
                    key_relationships = "\n".join(annotated_lines)

            # --- Novelty scoring: surface under-represented signals ---
            novelty_events_text = ""
            peripheral_novelty_text = ""
            try:
                if self.memory and self.memory.structured and self.memory.structured._available:
                    async with self.memory.structured._pool.acquire() as conn:
                        # All-time category distribution
                        cat_totals = await conn.fetch(
                            "SELECT category, COUNT(*) AS cnt FROM signals GROUP BY category"
                        )
                        total_signals_all = sum(r["cnt"] for r in cat_totals) if cat_totals else 0
                        cat_pct = {
                            r["category"]: r["cnt"] / max(total_signals_all, 1)
                            for r in cat_totals
                        } if cat_totals else {}

                        # All-time region distribution (via source geo_origin)
                        region_totals = await conn.fetch(
                            "SELECT s.geo_origin, COUNT(e.id) AS cnt "
                            "FROM signals e JOIN sources s ON e.source_id = s.id "
                            "GROUP BY s.geo_origin"
                        )
                        total_with_region = sum(r["cnt"] for r in region_totals) if region_totals else 0
                        region_pct = {
                            (r["geo_origin"] or "unknown"): r["cnt"] / max(total_with_region, 1)
                            for r in region_totals
                        } if region_totals else {}

                        # Recent signals (48h) with region info
                        recent_for_novelty = await conn.fetch(
                            "SELECT e.id, e.data->>'title' AS title, "
                            "e.category, e.created_at, "
                            "s.geo_origin "
                            "FROM signals e "
                            "LEFT JOIN sources s ON e.source_id = s.id "
                            "WHERE e.created_at > NOW() - INTERVAL '48 hours' "
                            "ORDER BY e.created_at DESC LIMIT 200"
                        )

                        # Entities seen in last 100 signals (for entity novelty)
                        known_entities: set[str] = set()
                        try:
                            known_rows = await conn.fetch(
                                "SELECT DISTINCT ep.canonical_name "
                                "FROM signal_entity_links eel "
                                "JOIN entity_profiles ep ON eel.entity_id = ep.id "
                                "WHERE eel.event_id IN ("
                                "  SELECT id FROM signals ORDER BY created_at DESC LIMIT 100"
                                ")"
                            )
                            known_entities = {r["canonical_name"] for r in known_rows}
                        except Exception:
                            pass

                        # Score each recent signal
                        scored = []
                        for ev in recent_for_novelty:
                            # Category novelty: rarer category = higher score
                            cat_freq = cat_pct.get(ev["category"], 0.5)
                            cat_novelty = 1.0 - cat_freq

                            # Region novelty: rarer region = higher score
                            region = ev["geo_origin"] or "unknown"
                            region_freq = region_pct.get(region, 0.5)
                            region_novelty = 1.0 - region_freq

                            # Entity novelty: check if signal links to unseen entities
                            entity_novelty = 0.0
                            try:
                                ev_entities = await conn.fetch(
                                    "SELECT ep.canonical_name "
                                    "FROM signal_entity_links eel "
                                    "JOIN entity_profiles ep ON eel.entity_id = ep.id "
                                    "WHERE eel.event_id = $1",
                                    ev["id"],
                                )
                                if ev_entities:
                                    unseen = sum(
                                        1 for r in ev_entities
                                        if r["canonical_name"] not in known_entities
                                    )
                                    entity_novelty = unseen / len(ev_entities)
                            except Exception:
                                pass

                            # Composite: weight category 40%, region 30%, entity 30%
                            novelty = round(
                                0.4 * cat_novelty + 0.3 * region_novelty + 0.3 * entity_novelty,
                                3,
                            )
                            scored.append({
                                "title": ev["title"],
                                "category": ev["category"],
                                "region": region,
                                "novelty": novelty,
                            })

                        scored.sort(key=lambda x: x["novelty"], reverse=True)
                        top_novel = scored[:15]

                        # Partition into primary-domain vs peripheral signals
                        primary_domains = self.config.agent.report_primary_domains
                        primary_lines: list[str] = []
                        peripheral_lines: list[str] = []
                        for item in top_novel:
                            line = (
                                f"- [novelty={item['novelty']:.2f}] [{item['category']}] "
                                f"({item['region']}) {item['title']}"
                            )
                            if item["category"] in primary_domains:
                                primary_lines.append(line)
                            else:
                                peripheral_lines.append(line)

                        novelty_events_text = "\n".join(primary_lines) if primary_lines else ""
                        peripheral_novelty_text = "\n".join(peripheral_lines) if peripheral_lines else ""
            except Exception:
                pass  # Best-effort — don't break the report if novelty scoring fails

            # Query active watches with recent triggers for the report
            watchlist_summary = ""
            try:
                async with self.structured._pool.acquire() as conn:
                    watch_rows = await conn.fetch(
                        "SELECT name, priority, trigger_count, last_triggered_at, data "
                        "FROM watchlist WHERE active = true AND trigger_count > 0 "
                        "ORDER BY trigger_count DESC LIMIT 15",
                    )
                    if watch_rows:
                        lines = []
                        for wr in watch_rows:
                            last = wr["last_triggered_at"]
                            last_str = last.strftime("%Y-%m-%d") if last else "never"
                            lines.append(
                                f"- [{wr['priority']}] {wr['name']}: "
                                f"{wr['trigger_count']} triggers (last: {last_str})"
                            )
                        watchlist_summary = "\n".join(lines)
            except Exception:
                pass

            report_messages = self.assembler.assemble_analysis_report_prompt(
                cycle_number=self.state.cycle_number,
                graph_summary=graph_summary,
                key_relationships=key_relationships,
                entity_profiles=entity_profiles_text,
                recent_events=recent_events,
                entity_count=entity_count,
                coverage_regions=coverage_regions,
                narrative=narrative_with_prev,
                novelty_events=novelty_events_text,
                peripheral_novelty=peripheral_novelty_text,
                watchlist_summary=watchlist_summary,
            )

            response = await self.llm.complete(
                report_messages,
                purpose="analysis_report",
            )

            report_content = response.content.strip()
            for token in ["<|end|>", "<|return|>"]:
                report_content = report_content.replace(token, "")

            # Store report in Redis
            report_data = {
                "cycle": self.state.cycle_number,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "content": report_content,
            }
            await self.memory.registers.set_json("latest_report", report_data)

            # Append to report history
            report_history = await self.memory.registers.get_json("report_history") or []
            report_history.append(report_data)
            # Keep last 20 reports
            if len(report_history) > 20:
                report_history = report_history[-20:]
            await self.memory.registers.set_json("report_history", report_history)

            # Archive to OpenSearch for permanent storage and search
            await self._archive_report_to_opensearch(
                cycle=self.state.cycle_number,
                timestamp=report_data["timestamp"],
                content=report_content,
                report_type="world_assessment",
            )

            # Also publish to outbound so it shows in messages
            self._outbox_messages.append(OutboxMessage(
                id=str(uuid4()),
                content=f"[ANALYSIS REPORT — Cycle {self.state.cycle_number}]\n\n{report_content}",
                cycle_number=self.state.cycle_number,
                metadata={"type": "analysis_report"},
            ))

            self.logger.log("analysis_report_complete",
                            report_length=len(report_content))

            # Compute and store operator scorecard
            await self._compute_scorecard()

            # Recompute source quality scores
            try:
                updated = await self.memory.structured.compute_source_quality_scores()
                if updated:
                    self.logger.log("source_quality_recomputed", sources_updated=updated)
            except Exception:
                pass

            # Extract proposed edges from report (relationship inference queue)
            await self._extract_proposed_edges(report_content, key_relationships)

        except Exception as e:
            self.logger.log_error(f"Analysis report generation failed: {e}")

    async def _archive_report_to_opensearch(self: AgentCycle, *, cycle: int,
                                             timestamp: str, content: str,
                                             report_type: str) -> None:
        """Archive a report document to OpenSearch for permanent storage."""
        if not self.opensearch or not self.opensearch.available:
            return
        try:
            doc = {
                "type": report_type,
                "cycle_number": cycle,
                "timestamp": timestamp,
                "content": content,
            }
            await self.opensearch.index_document(
                index=self._REPORT_INDEX,
                document=doc,
                doc_id=f"{report_type}-{cycle}",
            )
        except Exception:
            pass  # Best-effort — don't break the cycle if archiving fails

    async def _compute_scorecard(self: AgentCycle) -> None:
        """Compute operator scorecard with hard diagnostic telemetry.

        Collects coverage by region/category, entity link rate, fact freshness,
        source health, top entities, and graph stats. Stored in Redis for the
        UI to serve via /api/v2/scorecard.
        """
        scorecard: dict = {}
        try:
            if not (self.memory and self.memory.structured and self.memory.structured._available):
                self.logger.log("scorecard_skip", reason="structured store unavailable")
                return

            async with self.memory.structured._pool.acquire() as conn:
                # --- Coverage by category (last 48h) ---
                rows = await conn.fetch(
                    "SELECT category, COUNT(*) AS cnt FROM signals "
                    "WHERE created_at > NOW() - INTERVAL '48 hours' "
                    "GROUP BY category ORDER BY cnt DESC"
                )
                scorecard["coverage_by_category"] = {r["category"]: r["cnt"] for r in rows}

                # --- Coverage by region (source geo_origin, last 48h) ---
                rows = await conn.fetch(
                    "SELECT s.geo_origin, COUNT(e.id) AS cnt FROM signals e "
                    "JOIN sources s ON e.source_id = s.id "
                    "WHERE e.created_at > NOW() - INTERVAL '48 hours' "
                    "GROUP BY s.geo_origin ORDER BY cnt DESC"
                )
                scorecard["coverage_by_region"] = {
                    r["geo_origin"] or "unknown": r["cnt"] for r in rows
                }

                # --- Entity link rate ---
                total_signals = await conn.fetchval("SELECT COUNT(*) FROM signals") or 0
                linked_signals = await conn.fetchval(
                    "SELECT COUNT(DISTINCT event_id) FROM signal_entity_links"
                ) or 0
                scorecard["entity_link_rate"] = round(
                    linked_signals / max(total_signals, 1) * 100, 1
                )

                # --- Fact freshness ---
                total_facts = await conn.fetchval(
                    "SELECT COUNT(*) FROM facts WHERE superseded_by IS NULL"
                ) or 0
                stale_facts = await conn.fetchval(
                    "SELECT COUNT(*) FROM facts WHERE superseded_by IS NULL "
                    "AND source_cycle < $1",
                    self.state.cycle_number - 200,
                ) or 0
                scorecard["fact_freshness_pct"] = round(
                    (total_facts - stale_facts) / max(total_facts, 1) * 100, 1
                )
                scorecard["stale_facts"] = stale_facts

                # --- Source health ---
                rows = await conn.fetch(
                    "SELECT status, COUNT(*) AS cnt FROM sources GROUP BY status"
                )
                scorecard["source_health"] = {r["status"]: r["cnt"] for r in rows}

                # --- Source quality scores (top/bottom) ---
                try:
                    quality = await self.memory.structured.get_source_quality_summary(limit=5)
                    scorecard["source_quality"] = quality
                except Exception:
                    scorecard["source_quality"] = {}

                # --- Top entities by signal involvement ---
                rows = await conn.fetch(
                    "SELECT ep.canonical_name, COUNT(eel.event_id) AS event_count "
                    "FROM entity_profiles ep "
                    "JOIN signal_entity_links eel ON ep.id = eel.entity_id "
                    "GROUP BY ep.canonical_name ORDER BY event_count DESC LIMIT 10"
                )
                scorecard["top_entities"] = {
                    r["canonical_name"]: r["event_count"] for r in rows
                }

            # --- Graph stats (via Cypher, outside the PG connection) ---
            try:
                if self.memory.graph and self.memory.graph.available:
                    node_result = await self.memory.graph.execute_cypher(
                        "MATCH (n) RETURN count(n) AS cnt"
                    )
                    edge_result = await self.memory.graph.execute_cypher(
                        "MATCH ()-[r]->() RETURN count(r) AS cnt"
                    )
                    node_count = 0
                    edge_count = 0
                    if node_result and "cnt" in node_result[0]:
                        val = node_result[0]["cnt"]
                        node_count = int(val) if val is not None else 0
                    if edge_result and "cnt" in edge_result[0]:
                        val = edge_result[0]["cnt"]
                        edge_count = int(val) if val is not None else 0
                    scorecard["graph"] = {"nodes": node_count, "edges": edge_count}
            except Exception:
                scorecard["graph"] = {"nodes": 0, "edges": 0}

            # --- Timestamp ---
            scorecard["timestamp"] = datetime.now(timezone.utc).isoformat()

            # --- Store in Redis ---
            await self.memory.registers.set_json("scorecard", scorecard)
            await self.memory.registers.set(
                "scorecard_cycle", str(self.state.cycle_number)
            )

            self.logger.log("scorecard_complete",
                            categories=len(scorecard.get("coverage_by_category", {})),
                            regions=len(scorecard.get("coverage_by_region", {})),
                            entity_link_rate=scorecard.get("entity_link_rate"),
                            fact_freshness_pct=scorecard.get("fact_freshness_pct"))

        except Exception as e:
            self.logger.log_error(f"Scorecard computation failed: {e}")

    async def _extract_proposed_edges(
        self: AgentCycle,
        report_content: str,
        existing_relationships: str,
    ) -> None:
        """Extract relationship proposals from the report that aren't in the graph.

        Runs a focused LLM call to identify relationships asserted or implied in the
        report that don't appear in the existing graph edges. Stores proposals in the
        proposed_edges table for operator review. Auto-approves high-confidence proposals
        where both entities are already resolved.
        """
        if not report_content or not self.memory.structured or not self.memory.structured._available:
            return
        try:
            from ..llm.format import Message

            extract_messages = [
                Message(role="system", content=(
                    "reasoning: low\n\n"
                    "You extract structured relationship data from intelligence reports. "
                    "Output ONLY valid JSON — no markdown, no commentary."
                )),
                Message(role="user", content=(
                    "Below is an intelligence report and the current graph relationships.\n\n"
                    "## Current Graph Relationships\n"
                    f"{existing_relationships}\n\n"
                    "## Report\n"
                    f"{report_content[:4000]}\n\n"
                    "Identify relationships mentioned or strongly implied in the report "
                    "that are NOT already in the graph relationships above. For each, output:\n"
                    '{"proposals": [{"source": "EntityA", "target": "EntityB", '
                    '"rel_type": "HostileTo", "confidence": 0.8, '
                    '"evidence": "brief quote or reason from report"}]}\n\n'
                    "Use ONLY canonical relationship types: LeaderOf, HostileTo, AlliedWith, "
                    "SuppliesWeaponsTo, SanctionedBy, MemberOf, OperatesIn, LocatedIn, "
                    "OccupiedBy, SignatoryTo, TradesWith, BordersWith, FundedBy, AffiliatedWith, PartOf.\n"
                    "If no new relationships found, output: {\"proposals\": []}\n"
                    "Output ONLY the JSON object."
                )),
            ]

            response = await self.llm.complete(extract_messages, purpose="edge_extraction")
            content = response.content.strip()

            # Parse JSON from response
            import re
            json_match = re.search(r'\{.*\}', content, re.DOTALL)
            if not json_match:
                return
            proposals_data = json.loads(json_match.group())
            proposals = proposals_data.get("proposals", [])
            if not proposals:
                return

            # Resolve which entities exist in the graph
            resolved_entities: set[str] = set()
            try:
                async with self.memory.structured._pool.acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT LOWER(canonical_name) AS name FROM entity_profiles"
                    )
                    resolved_entities = {r["name"] for r in rows}
            except Exception:
                pass

            # Canonical relationship types for auto-approve validation
            canonical_rels = {
                "LeaderOf", "HostileTo", "AlliedWith", "SuppliesWeaponsTo",
                "SanctionedBy", "MemberOf", "OperatesIn", "LocatedIn",
                "OccupiedBy", "SignatoryTo", "TradesWith", "BordersWith",
                "FundedBy", "AffiliatedWith", "PartOf",
            }

            stored = 0
            async with self.memory.structured._pool.acquire() as conn:
                for p in proposals[:10]:  # Cap at 10 per cycle
                    source = p.get("source", "").strip()
                    target = p.get("target", "").strip()
                    rel_type = p.get("rel_type", "").strip()
                    confidence = float(p.get("confidence", 0.5))
                    evidence = p.get("evidence", "")[:500]

                    if not source or not target or not rel_type:
                        continue

                    # Check for duplicates
                    existing = await conn.fetchval(
                        "SELECT id FROM proposed_edges "
                        "WHERE LOWER(source_entity) = LOWER($1) "
                        "AND LOWER(target_entity) = LOWER($2) "
                        "AND relationship_type = $3 "
                        "AND status = 'pending'",
                        source, target, rel_type,
                    )
                    if existing:
                        continue

                    # Auto-approve if high confidence + both entities resolved + canonical type
                    both_resolved = (
                        source.lower() in resolved_entities
                        and target.lower() in resolved_entities
                    )
                    auto_approve = (
                        confidence >= 0.85
                        and both_resolved
                        and rel_type in canonical_rels
                    )

                    status = "approved" if auto_approve else "pending"

                    await conn.execute(
                        "INSERT INTO proposed_edges "
                        "(source_entity, target_entity, relationship_type, confidence, "
                        "evidence_text, source_cycle, status) "
                        "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                        source, target, rel_type, confidence,
                        evidence, self.state.cycle_number, status,
                    )
                    stored += 1

                    # If auto-approved and graph is available, commit to graph immediately
                    if auto_approve and self.memory.graph and self.memory.graph.available:
                        try:
                            await self.memory.graph.add_relationship(
                                source, target, rel_type,
                                {"source_cycle": self.state.cycle_number, "auto_proposed": True},
                            )
                        except Exception:
                            pass  # Edge commit failure is non-fatal

            if stored:
                self.logger.log("proposed_edges_extracted", count=stored)

        except Exception as e:
            self.logger.log_error(f"Edge extraction failed (non-fatal): {e}")
