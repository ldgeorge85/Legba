"""CURATE phase — intelligence curation: turn raw signals into analytical events.

JDL Level 2: Event curation and situation linking.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...shared.schemas.comms import InboxMessage

if TYPE_CHECKING:
    from ..cycle import AgentCycle


class CurateMixin:
    """Curate cycle: editorial judgment on raw signals, event creation, entity linking."""

    async def _curate(self: AgentCycle) -> None:
        """Curate cycle: review unclustered signals, refine auto-events, enrich entities."""
        self.logger.log_phase("curate")

        curate_context = await self._build_curate_context()

        from ..prompt import templates as _tpl
        allowed_tools = _tpl.CURATE_TOOLS

        inbox_messages = [InboxMessage(**m) for m in self.state.inbox_messages]
        curate_messages = self.assembler.assemble_curate_prompt(
            cycle_number=self.state.cycle_number,
            seed_goal=self.state.seed_goal,
            active_goals=[g.model_dump() for g in self._active_goals],
            curate_context=curate_context,
            allowed_tools=allowed_tools,
            inbox_messages=inbox_messages if inbox_messages else None,
            priority_context=getattr(self, "_priority_context", ""),
        )

        async def curate_executor(tool_name: str, arguments: dict) -> str:
            if tool_name not in allowed_tools:
                return (f"Tool '{tool_name}' is not available during curate cycles. "
                        f"Available tools: {', '.join(sorted(allowed_tools))}")
            return await self.executor.execute(tool_name, arguments)

        self._cycle_plan = "CURATE CYCLE: Review unclustered signals, refine auto-events, entity enrichment."

        try:
            self._final_response, self._conversation = await self.llm.reason_with_tools(
                messages=curate_messages,
                tool_executor=curate_executor,
                purpose="curate",
                max_steps=self.config.agent.max_reasoning_steps,
                stop_check=self._make_stop_checker(),
            )

            self.state.actions_taken = sum(
                1 for m in self._conversation
                if m.role == "user" and m.content.startswith("[Tool Result:")
            )

            self.logger.log("curate_complete",
                           actions=self.state.actions_taken,
                           response_length=len(self._final_response))
        except Exception as e:
            self._final_response = f"Curate cycle failed: {e}"
            self._conversation = []
            self.logger.log_error(f"Curate cycle failed: {e}")

    async def _build_curate_context(self: AgentCycle) -> str:
        """Gather signal/event data for the curate prompt."""
        lines = []
        try:
            async with self.memory.structured._pool.acquire() as conn:
                # 1. Unclustered signals (no linked event, not junk)
                unclustered = await conn.fetch("""
                    SELECT s.id, s.title, s.category, s.confidence,
                           s.source_url, s.created_at
                    FROM signals s
                    LEFT JOIN signal_event_links sel ON sel.signal_id = s.id
                    WHERE sel.signal_id IS NULL
                      AND s.category != 'other'
                    ORDER BY s.confidence DESC
                    LIMIT 20
                """)

                if unclustered:
                    lines.append("### Unclustered Signals (no linked event)")
                    lines.append(f"({len(unclustered)} shown, highest confidence first)\n")
                    for r in unclustered:
                        lines.append(
                            f"- **[{r['id']}]** ({r['category']}, conf={r['confidence']:.2f}) "
                            f"{r['title'][:120]}"
                        )
                    lines.append("")
                else:
                    lines.append("### Unclustered Signals\n(None found — all signals are linked to events)\n")

                # 2. Recent auto-created events with low signal count
                # Try to include lifecycle_status if the column exists
                try:
                    low_confidence_events = await conn.fetch("""
                        SELECT e.id, e.title, e.severity, e.event_type,
                               e.signal_count, e.confidence, e.created_at,
                               e.lifecycle_status
                        FROM events e
                        WHERE e.source_method = 'auto'
                          AND e.signal_count <= 2
                        ORDER BY e.created_at DESC
                        LIMIT 15
                    """)
                    _has_lifecycle = True
                except Exception:
                    low_confidence_events = await conn.fetch("""
                        SELECT e.id, e.title, e.severity, e.event_type,
                               e.signal_count, e.confidence, e.created_at
                        FROM events e
                        WHERE e.source_method = 'auto'
                          AND e.signal_count <= 2
                        ORDER BY e.created_at DESC
                        LIMIT 15
                    """)
                    _has_lifecycle = False

                if low_confidence_events:
                    lines.append("### Auto-Created Events (low signal count, need review)")
                    lines.append(f"({len(low_confidence_events)} shown)\n")
                    for r in low_confidence_events:
                        sev = r['severity'] or 'unset'
                        etype = r['event_type'] or 'unset'
                        lc = f", lifecycle={r['lifecycle_status']}" if _has_lifecycle and r.get('lifecycle_status') else ""
                        lines.append(
                            f"- **[{r['id']}]** (signals={r['signal_count']}, "
                            f"conf={r['confidence']:.2f}, sev={sev}, type={etype}{lc}) "
                            f"{r['title'][:120]}"
                        )
                    lines.append("")

                # 3. Trending events (high signal count)
                try:
                    trending = await conn.fetch("""
                        SELECT e.id, e.title, e.severity, e.event_type,
                               e.signal_count, e.confidence, e.lifecycle_status
                        FROM events e
                        WHERE e.signal_count > 2
                        ORDER BY e.signal_count DESC
                        LIMIT 5
                    """)
                    _trending_has_lifecycle = True
                except Exception:
                    trending = await conn.fetch("""
                        SELECT e.id, e.title, e.severity, e.event_type,
                               e.signal_count, e.confidence
                        FROM events e
                        WHERE e.signal_count > 2
                        ORDER BY e.signal_count DESC
                        LIMIT 5
                    """)
                    _trending_has_lifecycle = False

                if trending:
                    lines.append("### Trending Events (high signal count)")
                    for r in trending:
                        sev = r['severity'] or 'unset'
                        lc = f", lifecycle={r['lifecycle_status']}" if _trending_has_lifecycle and r.get('lifecycle_status') else ""
                        lines.append(
                            f"- **[{r['id']}]** (signals={r['signal_count']}, sev={sev}{lc}) "
                            f"{r['title'][:120]}"
                        )
                    lines.append("")

                # 4. Recent watch triggers
                watch_triggers = await conn.fetch("""
                    SELECT wt.watch_name, wt.event_title AS signal_title,
                           wt.priority, wt.triggered_at,
                           w.name AS watch_name_full
                    FROM watch_triggers wt
                    JOIN watchlist w ON w.id = wt.watch_id
                    WHERE wt.triggered_at > NOW() - INTERVAL '24 hours'
                    ORDER BY wt.triggered_at DESC
                    LIMIT 10
                """)

                if watch_triggers:
                    lines.append("### Recent Watch Triggers (last 24h)")
                    for wt in watch_triggers:
                        pri = wt['priority'] or 'normal'
                        name = wt['watch_name_full'] or wt['watch_name'] or 'unknown'
                        title = (wt['signal_title'] or '')[:120]
                        ts = wt['triggered_at'].isoformat() if wt['triggered_at'] else '?'
                        lines.append(f'- [{pri}] "{name}" triggered by "{title}" at {ts}')
                    lines.append("")

                # 5. Overall counts for context
                total_signals = await conn.fetchval("SELECT count(*) FROM signals")
                total_events = await conn.fetchval("SELECT count(*) FROM events")
                unlinked_count = await conn.fetchval("""
                    SELECT count(*) FROM signals s
                    LEFT JOIN signal_event_links sel ON sel.signal_id = s.id
                    WHERE sel.signal_id IS NULL
                """)

                lines.append("### Data Overview")
                lines.append(f"- Total signals: {total_signals}")
                lines.append(f"- Total derived events: {total_events}")
                lines.append(f"- Unlinked signals: {unlinked_count}")

        except Exception as e:
            lines.append(f"(Could not load curate context: {e})")

        return "\n".join(lines)
