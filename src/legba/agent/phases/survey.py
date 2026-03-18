"""SURVEY phase — analytical desk work replacing the old NORMAL cycle.

Reviews recent events, builds graph relationships, updates situations,
evaluates hypotheses, follows journal leads. No collection, no code modification.
Rate-limited external access (max 2 http_request calls) for verification only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...shared.schemas.comms import InboxMessage

if TYPE_CHECKING:
    from ..cycle import AgentCycle

# Track http_request calls per cycle for rate limiting.
_SURVEY_HTTP_LIMIT = 2


class SurveyMixin:
    """Survey cycle: analytical desk work — situation updates, graph building,
    hypothesis evaluation, opportunistic curation."""

    async def _survey(self: AgentCycle) -> None:
        """Survey cycle: review recent events, build relationships, update situations."""
        self.logger.log_phase("survey")

        survey_context = await self._build_survey_context()

        from ..prompt import templates as _tpl
        allowed_tools = _tpl.SURVEY_TOOLS

        inbox_messages = [InboxMessage(**m) for m in self.state.inbox_messages]
        survey_messages = self.assembler.assemble_survey_prompt(
            cycle_number=self.state.cycle_number,
            seed_goal=self.state.seed_goal,
            active_goals=[g.model_dump() for g in self._active_goals],
            survey_context=survey_context,
            allowed_tools=allowed_tools,
            inbox_messages=inbox_messages if inbox_messages else None,
        )

        # Rate-limited executor: http_request capped at _SURVEY_HTTP_LIMIT per cycle
        http_count = 0

        async def survey_executor(tool_name: str, arguments: dict) -> str:
            nonlocal http_count
            if tool_name not in allowed_tools:
                return (f"Tool '{tool_name}' is not available during survey cycles. "
                        f"Available tools: {', '.join(sorted(allowed_tools))}")
            if tool_name == "http_request":
                http_count += 1
                if http_count > _SURVEY_HTTP_LIMIT:
                    return (f"http_request limit reached ({_SURVEY_HTTP_LIMIT} per survey cycle). "
                            f"Use RESEARCH or SYNTHESIZE cycles for deeper external access.")
            return await self.executor.execute(tool_name, arguments)

        self._cycle_plan = (
            "SURVEY CYCLE: Review recent events, update situations, build graph relationships, "
            "evaluate hypotheses, follow investigation leads."
        )

        try:
            self._final_response, self._conversation = await self.llm.reason_with_tools(
                messages=survey_messages,
                tool_executor=survey_executor,
                purpose="survey",
                max_steps=self.config.agent.max_reasoning_steps,
                stop_check=self._make_stop_checker(),
            )

            self.state.actions_taken = sum(
                1 for m in self._conversation
                if m.role == "user" and m.content.startswith("[Tool Result:")
            )

            self.logger.log("survey_complete",
                           actions=self.state.actions_taken,
                           http_calls=http_count,
                           response_length=len(self._final_response))
        except Exception as e:
            self._final_response = f"Survey cycle failed: {e}"
            self._conversation = []
            self.logger.log_error(f"Survey cycle failed: {e}")

    async def _build_survey_context(self: AgentCycle) -> str:
        """Build context for SURVEY: recent events, situations, leads, predictions."""
        lines = []
        try:
            async with self.memory.structured._pool.acquire() as conn:
                # 1. Recent high-confidence derived events (last 24h)
                recent_events = await conn.fetch("""
                    SELECT e.id, e.title, e.severity, e.event_type,
                           e.signal_count, e.confidence, e.created_at
                    FROM events e
                    WHERE e.created_at > NOW() - INTERVAL '24 hours'
                    ORDER BY e.created_at DESC
                    LIMIT 20
                """)
                if recent_events:
                    lines.append("### Recent Events (last 24h)")
                    for r in recent_events:
                        sev = r['severity'] or 'unset'
                        etype = r['event_type'] or 'unset'
                        lines.append(
                            f"- **[{r['id']}]** ({sev}, {etype}, signals={r['signal_count']}) "
                            f"{r['title'][:120]}"
                        )
                    lines.append("")

                # 2. Active situations with recent activity
                situations = await conn.fetch("""
                    SELECT s.id, s.name, s.status, s.event_count,
                           s.intensity_score, s.updated_at
                    FROM situations s
                    WHERE s.status != 'resolved'
                    ORDER BY s.intensity_score DESC
                    LIMIT 10
                """)
                if situations:
                    lines.append("### Active Situations")
                    for s in situations:
                        lines.append(
                            f"- **{s['name']}** [{s['status']}] — "
                            f"{s['event_count']} events, intensity {s['intensity_score']:.2f}"
                        )
                    lines.append("")

                # 3. Events not linked to any situation
                unlinked_events = await conn.fetch("""
                    SELECT e.id, e.title, e.severity, e.signal_count
                    FROM events e
                    LEFT JOIN situation_events se ON se.event_id = e.id
                    WHERE se.event_id IS NULL
                    ORDER BY e.signal_count DESC
                    LIMIT 10
                """)
                if unlinked_events:
                    lines.append("### Events Without Situation Links")
                    for r in unlinked_events:
                        sev = r['severity'] or 'unset'
                        lines.append(
                            f"- **[{r['id']}]** ({sev}, signals={r['signal_count']}) "
                            f"{r['title'][:120]}"
                        )
                    lines.append("")

                # 4. Active predictions needing evidence check
                predictions = await conn.fetch("""
                    SELECT id, data FROM predictions
                    WHERE (data->>'status') = 'active'
                    ORDER BY created_at DESC
                    LIMIT 5
                """)
                if predictions:
                    lines.append("### Active Predictions (check against new evidence)")
                    for p in predictions:
                        import json
                        d = p['data'] if isinstance(p['data'], dict) else json.loads(p['data'])
                        desc = d.get('description', d.get('hypothesis', '?'))[:150]
                        lines.append(f"- **[{p['id']}]** {desc}")
                    lines.append("")

                # 5. Active hypotheses needing evidence evaluation
                hypotheses = await conn.fetch("""
                    SELECT h.id, h.thesis, h.counter_thesis, h.evidence_balance,
                           h.status, h.last_evaluated_cycle,
                           array_length(h.supporting_signals, 1) as support_count,
                           array_length(h.refuting_signals, 1) as refute_count,
                           s.name as situation_name
                    FROM hypotheses h
                    LEFT JOIN situations s ON s.id = h.situation_id
                    WHERE h.status = 'active'
                    ORDER BY h.updated_at DESC
                    LIMIT 10
                """)
                if hypotheses:
                    lines.append("### Active Hypotheses (evaluate against new evidence)")
                    for h in hypotheses:
                        sup = h['support_count'] or 0
                        ref = h['refute_count'] or 0
                        sit = h['situation_name'] or 'unlinked'
                        lines.append(
                            f"- **[{h['id']}]** ({sit}) balance={h['evidence_balance']:+d} "
                            f"({sup} for, {ref} against)"
                        )
                        lines.append(f"  Thesis: {h['thesis'][:120]}")
                        lines.append(f"  Counter: {h['counter_thesis'][:120]}")
                    lines.append("")

                # 6. Recent watch triggers
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
                        lines.append(f'- [{pri}] "{name}" triggered by "{title}"')
                    lines.append("")

                # 6. Uncurated signal count (for awareness)
                uncurated = await conn.fetchval("""
                    SELECT count(*) FROM signals s
                    LEFT JOIN signal_event_links sel ON sel.signal_id = s.id
                    WHERE sel.signal_id IS NULL
                      AND s.category != 'other'
                """)
                # Cache for dynamic CURATE promotion check
                self._uncurated_count = uncurated or 0

                # 7. Overview counts
                total_signals = await conn.fetchval("SELECT count(*) FROM signals")
                total_events = await conn.fetchval("SELECT count(*) FROM events")

                lines.append("### Data Overview")
                lines.append(f"- Total signals: {total_signals}")
                lines.append(f"- Total derived events: {total_events}")
                lines.append(f"- Uncurated signals: {uncurated}")

        except Exception as e:
            lines.append(f"(Could not load survey context: {e})")

        # Journal investigation leads
        try:
            if self.memory and self.memory.registers:
                leads = await self.memory.registers.get_json("journal_leads")
                if leads and isinstance(leads, list) and leads:
                    lines.append("\n### Journal Investigation Leads")
                    for lead in leads[:8]:
                        if isinstance(lead, dict):
                            lines.append(f"- {lead.get('lead', lead.get('text', str(lead)))[:150]}")
                        else:
                            lines.append(f"- {str(lead)[:150]}")
        except Exception:
            pass

        return "\n".join(lines)
