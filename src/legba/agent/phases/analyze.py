"""ANALYZE phase — analytical tools, pattern detection, metrics, differential reporting.

JDL Level 2: Pattern detection and structural analysis.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ...shared.schemas.comms import InboxMessage

if TYPE_CHECKING:
    from ..cycle import AgentCycle

_ANALYSIS_SNAPSHOT_KEY = "analysis_snapshot"


class AnalyzeMixin:
    """Analysis cycle: pattern detection, anomaly detection, graph mining."""

    def _is_analysis_cycle(self: AgentCycle) -> bool:
        """Check if this is an analysis cycle.

        Runs every ANALYSIS_INTERVAL cycles, but yields to EVOLVE,
        INTROSPECTION, and SYNTHESIZE.
        """
        from . import ANALYSIS_INTERVAL
        cn = self.state.cycle_number
        return (ANALYSIS_INTERVAL > 0
                and cn > 0
                and cn % ANALYSIS_INTERVAL == 0
                and not self._is_evolve_cycle()
                and not self._is_introspection_cycle()
                and not self._is_synthesize_cycle())

    async def _analyze(self: AgentCycle) -> None:
        """Analysis cycle: run analytical tools on accumulated data."""
        self.logger.log_phase("analyze")

        analysis_context = await self._build_analysis_context()

        from ..prompt import templates as _tpl
        allowed_tools = _tpl.ANALYSIS_TOOLS

        inbox_messages = [InboxMessage(**m) for m in self.state.inbox_messages]
        analysis_messages = self.assembler.assemble_analysis_cycle_prompt(
            cycle_number=self.state.cycle_number,
            seed_goal=self.state.seed_goal,
            active_goals=[g.model_dump() for g in self._active_goals],
            analysis_context=analysis_context,
            allowed_tools=allowed_tools,
            inbox_messages=inbox_messages if inbox_messages else None,
            priority_context=getattr(self, "_priority_context", ""),
        )

        async def analysis_executor(tool_name: str, arguments: dict) -> str:
            if tool_name not in allowed_tools:
                return (f"Tool '{tool_name}' is not available during analysis cycles. "
                        f"Available tools: {', '.join(sorted(allowed_tools))}")
            return await self.executor.execute(tool_name, arguments)

        self._cycle_plan = "ANALYSIS CYCLE: Pattern detection, graph mining, anomaly detection, trend analysis."

        try:
            try:
                self._final_response, self._conversation = await self.llm.reason_with_tools(
                    messages=analysis_messages,
                    tool_executor=analysis_executor,
                    purpose="analysis",
                    max_steps=self.config.agent.max_reasoning_steps,
                    stop_check=self._make_stop_checker(),
                )
            except Exception as first_err:
                # Retry once with reduced context
                self.logger.log("analysis_retry",
                               reason=f"First attempt failed: {first_err}")
                self.logger.logger.warning(
                    "Analysis cycle first attempt failed, retrying with reduced context: %s",
                    first_err,
                )
                reduced_context = analysis_context[:4000]
                if len(analysis_context) > 4000:
                    reduced_context += "\n(... truncated for retry)"
                analysis_messages = self.assembler.assemble_analysis_cycle_prompt(
                    cycle_number=self.state.cycle_number,
                    seed_goal=self.state.seed_goal,
                    active_goals=[g.model_dump() for g in self._active_goals],
                    analysis_context=reduced_context,
                    allowed_tools=allowed_tools,
                    inbox_messages=inbox_messages if inbox_messages else None,
                    priority_context=getattr(self, "_priority_context", ""),
                )
                self._final_response, self._conversation = await self.llm.reason_with_tools(
                    messages=analysis_messages,
                    tool_executor=analysis_executor,
                    purpose="analysis_retry",
                    max_steps=self.config.agent.max_reasoning_steps,
                    stop_check=self._make_stop_checker(),
                )

            self.state.actions_taken = sum(
                1 for m in self._conversation
                if m.role == "user" and m.content.startswith("[Tool Result:")
            )

            # Store analysis snapshot for differential reporting next cycle
            await self._store_analysis_snapshot()

            self.logger.log("analysis_complete",
                           actions=self.state.actions_taken,
                           response_length=len(self._final_response))
        except Exception as e:
            self._final_response = f"Analysis cycle failed: {e}"
            self._conversation = []
            self.logger.log_error(f"Analysis cycle failed: {e}")

    async def _build_analysis_context(self: AgentCycle) -> str:
        """Build context data for the analysis prompt, including differential report."""
        lines = []
        current_snapshot = {}
        try:
            async with self.memory.structured._pool.acquire() as conn:
                # Signal counts by category
                cat_rows = await conn.fetch("""
                    SELECT category, count(*) as cnt
                    FROM signals
                    GROUP BY category
                    ORDER BY cnt DESC
                """)
                cat_dist = {r["category"]: r["cnt"] for r in cat_rows} if cat_rows else {}
                current_snapshot["categories"] = cat_dist
                if cat_dist:
                    lines.append("### Signal Distribution by Category")
                    for cat, cnt in cat_dist.items():
                        lines.append(f"- {cat}: {cnt}")
                    lines.append("")

                # Top entities by signal involvement
                entity_rows = await conn.fetch("""
                    SELECT ep.canonical_name, ep.entity_type,
                           COUNT(eel.event_id) as event_count
                    FROM entity_profiles ep
                    LEFT JOIN signal_entity_links eel ON eel.entity_id = ep.id
                    GROUP BY ep.id, ep.canonical_name, ep.entity_type
                    HAVING COUNT(eel.event_id) > 0
                    ORDER BY COUNT(eel.event_id) DESC
                    LIMIT 20
                """)
                top_entities = {}
                if entity_rows:
                    lines.append("### Top Entities by Signal Involvement")
                    for r in entity_rows:
                        top_entities[r["canonical_name"]] = r["event_count"]
                        lines.append(f"- {r['canonical_name']} ({r['entity_type']}): {r['event_count']} signals")
                    lines.append("")
                current_snapshot["top_entities"] = top_entities

                # Total counts for threshold checking
                total_signals = await conn.fetchval("SELECT count(*) FROM signals")
                total_entities = await conn.fetchval("SELECT count(*) FROM entity_profiles")
                total_rels = 0
                try:
                    rel_row = await conn.fetchrow(
                        "SELECT * FROM cypher('legba_graph', $$ MATCH ()-[r]->() RETURN count(r) as cnt $$) as (cnt agtype)"
                    )
                    if rel_row:
                        import re
                        total_rels = int(re.sub(r'[^0-9]', '', str(rel_row['cnt'])))
                except Exception:
                    pass

                current_snapshot["totals"] = {
                    "signals": total_signals,
                    "entities": total_entities,
                    "relationships": total_rels,
                }

                lines.append("### Data Thresholds")
                lines.append(f"- Total signals: {total_signals} {'(enough for anomaly_detect)' if total_signals >= 30 else '(need 30+ for anomaly_detect)'}")
                lines.append(f"- Total entities: {total_entities}")
                lines.append(f"- Total relationships: {total_rels} {'(enough for graph_analyze)' if total_rels >= 20 else '(need 20+ for graph_analyze)'}")

                # Active situations
                sit_data = []
                try:
                    sit_rows = await conn.fetch(
                        "SELECT name, status, event_count, intensity_score FROM situations WHERE status != 'resolved' ORDER BY intensity_score DESC LIMIT 10"
                    )
                    if sit_rows:
                        lines.append("\n### Active Situations")
                        for r in sit_rows:
                            sit_data.append({"name": r["name"], "status": r["status"],
                                             "events": r["event_count"], "intensity": float(r["intensity_score"])})
                            lines.append(f"- {r['name']} [{r['status']}] — {r['event_count']} events, intensity {r['intensity_score']:.2f}")
                except Exception:
                    pass
                current_snapshot["situations"] = sit_data

                # Active watchlist trigger summary
                try:
                    trig_rows = await conn.fetch("""
                        SELECT w.name, w.priority, w.trigger_count
                        FROM watchlist w
                        WHERE w.active = true AND w.trigger_count > 0
                        ORDER BY w.trigger_count DESC LIMIT 10
                    """)
                    if trig_rows:
                        lines.append("\n### Watchlist Trigger Summary")
                        for r in trig_rows:
                            lines.append(f"- {r['name']} [{r['priority']}]: {r['trigger_count']} triggers")
                except Exception:
                    pass

        except Exception as e:
            lines.append(f"(Could not load analysis context: {e})")

        # --- Adversarial context: flag coordinated/suspicious signals ---
        try:
            from legba.shared.adversarial_context import get_adversarial_summary
            adv_summary = await get_adversarial_summary(self.memory.structured._pool)
            if adv_summary:
                lines.append("")
                lines.append(adv_summary)
        except Exception:
            pass  # Graceful degradation

        # --- Differential reporting: compare against previous snapshot ---
        try:
            if self.memory and self.memory.registers:
                prev = await self.memory.registers.get_json(_ANALYSIS_SNAPSHOT_KEY)
                if prev:
                    diff_lines = self._compute_analysis_diff(prev, current_snapshot)
                    if diff_lines:
                        lines.append("\n### Changes Since Last Analysis")
                        lines.extend(diff_lines)
                # Stash current snapshot for _store_analysis_snapshot
                self._analysis_snapshot = current_snapshot
        except Exception:
            pass

        return "\n".join(lines)

    def _compute_analysis_diff(self: AgentCycle, prev: dict, curr: dict) -> list[str]:
        """Compare two analysis snapshots and highlight changes."""
        diff = []

        # Total count changes
        prev_totals = prev.get("totals", {})
        curr_totals = curr.get("totals", {})
        for key, label in [("signals", "signals"), ("entities", "entities"), ("relationships", "relationships")]:
            # Backward compat: old snapshots stored signal count under "events"
            if key == "signals" and key not in prev_totals:
                old_val = prev_totals.get("events", 0)
            else:
                old_val = prev_totals.get(key, 0)
            new_val = curr_totals.get(key, 0)
            delta = new_val - old_val
            if delta > 0:
                diff.append(f"- +{delta} new {label} (was {old_val}, now {new_val})")
            elif delta < 0:
                diff.append(f"- {delta} {label} (was {old_val}, now {new_val})")

        # Category distribution changes
        prev_cats = prev.get("categories", {})
        curr_cats = curr.get("categories", {})
        for cat in set(list(prev_cats.keys()) + list(curr_cats.keys())):
            old_cnt = prev_cats.get(cat, 0)
            new_cnt = curr_cats.get(cat, 0)
            delta = new_cnt - old_cnt
            if delta >= 3:
                diff.append(f"- Category '{cat}' grew by {delta} signals")
            elif cat not in prev_cats and new_cnt > 0:
                diff.append(f"- NEW category '{cat}' with {new_cnt} signals")

        # New top entities
        prev_ents = set(prev.get("top_entities", {}).keys())
        curr_ents = set(curr.get("top_entities", {}).keys())
        new_ents = curr_ents - prev_ents
        if new_ents:
            diff.append(f"- New top entities: {', '.join(sorted(new_ents)[:5])}")

        # Situation status changes
        prev_sits = {s["name"]: s for s in prev.get("situations", [])}
        curr_sits = {s["name"]: s for s in curr.get("situations", [])}
        for name in curr_sits:
            if name not in prev_sits:
                diff.append(f"- NEW situation: {name}")
            elif curr_sits[name].get("status") != prev_sits[name].get("status"):
                diff.append(f"- Situation '{name}' status changed: "
                            f"{prev_sits[name].get('status')} -> {curr_sits[name].get('status')}")
            elif curr_sits[name].get("events", 0) > prev_sits[name].get("events", 0) + 2:
                delta = curr_sits[name]["events"] - prev_sits[name]["events"]
                diff.append(f"- Situation '{name}' gained {delta} events")

        return diff

    async def _store_analysis_snapshot(self: AgentCycle) -> None:
        """Store current analysis metrics to Redis for next cycle's diff."""
        try:
            snapshot = getattr(self, "_analysis_snapshot", None)
            if snapshot and self.memory and self.memory.registers:
                snapshot["cycle"] = self.state.cycle_number
                await self.memory.registers.set_json(_ANALYSIS_SNAPSHOT_KEY, snapshot)
        except Exception:
            pass
