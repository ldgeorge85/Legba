"""SYNTHESIZE phase — deep-dive investigation into a single situation or thread.

Picks ONE investigation target, builds a coherent narrative, generates falsifiable
predictions. Produces a named deliverable: a Situation Brief stored alongside reports.
Tracks recently investigated threads to prevent rabbit-holing.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ...shared.schemas.comms import InboxMessage

if TYPE_CHECKING:
    from ..cycle import AgentCycle

log = logging.getLogger(__name__)

_SYNTH_HISTORY_KEY = "synthesize_history"
_SITUATION_BRIEFS_KEY = "legba:situation_briefs"
_MAX_SYNTH_HISTORY = 10


class SynthesizeMixin:
    """Synthesize cycle: deep-dive investigation into a single situation or thread."""

    def _is_synthesize_cycle(self: AgentCycle) -> bool:
        """Check if this is a synthesize cycle.

        Runs every SYNTHESIZE_INTERVAL cycles, but NOT on evolve or introspection cycles.
        """
        from . import SYNTHESIZE_INTERVAL
        cn = self.state.cycle_number
        return (SYNTHESIZE_INTERVAL > 0
                and cn > 0
                and cn % SYNTHESIZE_INTERVAL == 0
                and not self._is_evolve_cycle()
                and not self._is_introspection_cycle())

    async def _synthesize(self: AgentCycle) -> None:
        """Deep-dive: pick one thread, build narrative, generate predictions, produce brief."""
        self.logger.log_phase("synthesize")

        synthesize_context = await self._build_synthesize_context()

        from ..prompt import templates as _tpl
        allowed_tools = _tpl.SYNTHESIZE_TOOLS

        inbox_messages = [InboxMessage(**m) for m in self.state.inbox_messages]
        synth_messages = self.assembler.assemble_synthesize_prompt(
            cycle_number=self.state.cycle_number,
            seed_goal=self.state.seed_goal,
            active_goals=[g.model_dump() for g in self._active_goals],
            synthesize_context=synthesize_context,
            allowed_tools=allowed_tools,
            inbox_messages=inbox_messages if inbox_messages else None,
        )

        async def synth_executor(tool_name: str, arguments: dict) -> str:
            if tool_name not in allowed_tools:
                return (f"Tool '{tool_name}' is not available during synthesize cycles. "
                        f"Available tools: {', '.join(sorted(allowed_tools))}")
            return await self.executor.execute(tool_name, arguments)

        self._cycle_plan = (
            "SYNTHESIZE CYCLE: Deep-dive investigation — pick one thread, "
            "build narrative, generate predictions, produce situation brief."
        )

        try:
            self._final_response, self._conversation = await self.llm.reason_with_tools(
                messages=synth_messages,
                tool_executor=synth_executor,
                purpose="synthesize",
                max_steps=self.config.agent.max_reasoning_steps,
                stop_check=self._make_stop_checker(),
            )

            self.state.actions_taken = sum(
                1 for m in self._conversation
                if m.role == "user" and m.content.startswith("[Tool Result:")
            )

            # Generate a clean situation brief via dedicated LLM call
            # (the final_response from the tool loop is often just closing actions)
            await self._generate_situation_brief()

            # Track which thread was investigated for rotation
            await self._update_synth_history()

            self.logger.log("synthesize_complete",
                           actions=self.state.actions_taken,
                           response_length=len(self._final_response))
        except Exception as e:
            self._final_response = f"Synthesize cycle failed: {e}"
            self._conversation = []
            self.logger.log_error(f"Synthesize cycle failed: {e}")

    async def _build_synthesize_context(self: AgentCycle) -> str:
        """Build ranked candidate threads for investigation."""
        lines = []

        # Recently investigated threads (anti-rabbit-holing)
        recent_threads = []
        try:
            if self.memory and self.memory.registers:
                history = await self.memory.registers.get_json(_SYNTH_HISTORY_KEY)
                if history and isinstance(history, list):
                    recent_threads = history[:_MAX_SYNTH_HISTORY]
                    if recent_threads:
                        lines.append("### Recently Investigated Threads (DO NOT re-investigate unless materially changed)")
                        for entry in recent_threads[:5]:
                            topic = entry.get("topic", "?")
                            cycle = entry.get("cycle", "?")
                            lines.append(f"- Cycle {cycle}: {topic}")
                        lines.append("")
        except Exception:
            pass

        recent_thread_topics = {e.get("topic", "").lower() for e in recent_threads[:3]}

        try:
            async with self.memory.structured._pool.acquire() as conn:
                # Candidate situations ranked by novelty and intensity
                situations = await conn.fetch("""
                    SELECT s.id, s.name, s.status, s.event_count,
                           s.intensity_score, s.updated_at,
                           (SELECT count(*) FROM situation_events se
                            JOIN events e ON e.id = se.event_id
                            WHERE se.situation_id = s.id
                              AND e.created_at > NOW() - INTERVAL '48 hours'
                           ) AS recent_event_count
                    FROM situations s
                    WHERE s.status != 'resolved'
                    ORDER BY recent_event_count DESC, s.intensity_score DESC
                    LIMIT 10
                """)

                if situations:
                    lines.append("### Candidate Investigation Targets")
                    lines.append("(Ranked by recent activity and intensity. Pick ONE.)\n")
                    for i, s in enumerate(situations, 1):
                        name = s['name']
                        recently_done = name.lower() in recent_thread_topics
                        tag = " [RECENTLY INVESTIGATED — skip unless materially changed]" if recently_done else ""
                        lines.append(
                            f"{i}. **{name}** [{s['status']}] — "
                            f"{s['event_count']} total events, {s['recent_event_count']} in last 48h, "
                            f"intensity {s['intensity_score']:.2f}{tag}"
                        )
                    lines.append("")

                # Active predictions that could be evaluated
                predictions = await conn.fetch("""
                    SELECT id, data FROM predictions
                    WHERE (data->>'status') = 'active'
                    ORDER BY created_at DESC
                    LIMIT 8
                """)
                if predictions:
                    lines.append("### Unresolved Predictions")
                    for p in predictions:
                        d = p['data'] if isinstance(p['data'], dict) else json.loads(p['data'])
                        desc = d.get('description', d.get('hypothesis', '?'))[:150]
                        lines.append(f"- **[{p['id']}]** {desc}")
                    lines.append("")

                # High-activity entities (potential investigation anchors)
                entity_rows = await conn.fetch("""
                    SELECT ep.canonical_name, ep.entity_type,
                           COUNT(sel.signal_id) as signal_count
                    FROM entity_profiles ep
                    JOIN signal_entity_links sel ON sel.entity_id = ep.id
                    JOIN signals s ON s.id = sel.signal_id
                    WHERE s.created_at > NOW() - INTERVAL '48 hours'
                    GROUP BY ep.id, ep.canonical_name, ep.entity_type
                    ORDER BY COUNT(sel.signal_id) DESC
                    LIMIT 10
                """)
                if entity_rows:
                    lines.append("### High-Activity Entities (last 48h)")
                    for r in entity_rows:
                        lines.append(
                            f"- {r['canonical_name']} ({r['entity_type']}): "
                            f"{r['signal_count']} signals"
                        )
                    lines.append("")

        except Exception as e:
            lines.append(f"(Could not load synthesize context: {e})")

        # Journal leads that map to investigation threads
        try:
            if self.memory and self.memory.registers:
                leads = await self.memory.registers.get_json("journal_leads")
                if leads and isinstance(leads, list):
                    lines.append("### Journal Investigation Leads")
                    for lead in leads[:6]:
                        if isinstance(lead, dict):
                            lines.append(f"- {lead.get('lead', lead.get('text', str(lead)))[:150]}")
                        else:
                            lines.append(f"- {str(lead)[:150]}")
        except Exception:
            pass

        return "\n".join(lines)

    async def _generate_situation_brief(self: AgentCycle) -> None:
        """Generate a clean situation brief via dedicated LLM call, then store it."""
        try:
            if not self.llm or not self.memory or not self.memory.registers:
                return

            # Build a summary of what happened during the tool loop
            working_mem = self.llm.working_memory.full_text() if self.llm.working_memory else ""
            conversation_summary = ""
            for msg in (self._conversation or []):
                if msg.role == "assistant" and not msg.content.startswith("{"):
                    conversation_summary += msg.content[:2000] + "\n"
                elif msg.role == "user" and msg.content.startswith("[Tool Result:"):
                    conversation_summary += msg.content[:1000] + "\n"

            # Truncate to fit context
            if len(conversation_summary) > 15000:
                conversation_summary = conversation_summary[:15000] + "\n(... truncated)"

            from ..llm.format import Message
            brief_messages = [
                Message(role="system", content=(
                    "reasoning: high\n\n"
                    "You are Legba — autonomous intelligence analyst. Produce a Situation Brief "
                    "based on the investigation data below. Use markdown. Follow the section "
                    "structure exactly. Be specific — cite entity names, event IDs, relationships."
                )),
                Message(role="user", content=(
                    f"You just completed a SYNTHESIZE investigation (cycle {self.state.cycle_number}). "
                    f"Here is what you found:\n\n"
                    f"## Working Memory\n{working_mem[:5000]}\n\n"
                    f"## Investigation Data\n{conversation_summary}\n\n"
                    "Now produce your Situation Brief. Format:\n\n"
                    "# Legba Situation Brief: [Topic]\n\n"
                    "## Thesis\nOne-sentence summary.\n\n"
                    "## Evidence\nKey signals, events, relationships. Cite specifics.\n\n"
                    "## Competing Hypotheses\nAlternative explanations with relative likelihood.\n\n"
                    "## Predictions\nFalsifiable near-term predictions.\n\n"
                    "## Unknowns\nWhat you don't know and what would resolve it.\n\n"
                    "## Recommendations\nFollow-up actions for SURVEY and RESEARCH cycles."
                )),
            ]

            response = await self.llm.complete(
                brief_messages,
                purpose="situation_brief",
                max_tokens=4000,
            )

            brief_content = response.content.strip()
            if not brief_content:
                return

            brief = {
                "cycle": self.state.cycle_number,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "content": brief_content,
            }

            # Extract title
            for line in brief_content.split("\n"):
                stripped = line.strip().lstrip("#").strip()
                if stripped.lower().startswith("legba situation brief:"):
                    brief["title"] = stripped
                    break
            else:
                brief["title"] = f"Situation Brief — Cycle {self.state.cycle_number}"

            # Store to Redis list (newest first, capped)
            redis = self.memory.registers._redis
            await redis.lpush(_SITUATION_BRIEFS_KEY, json.dumps(brief, default=str))
            await redis.ltrim(_SITUATION_BRIEFS_KEY, 0, 19)

            # Archive to OpenSearch
            if self.opensearch and self.opensearch._client:
                try:
                    await self.opensearch._client.index(
                        index="legba-situation-briefs",
                        body=brief,
                    )
                except Exception:
                    pass

            self.logger.log("situation_brief_stored", title=brief.get("title", "?"))
        except Exception as e:
            log.warning("Failed to generate situation brief: %s", e)

    async def _update_synth_history(self: AgentCycle) -> None:
        """Track which thread was investigated for rotation."""
        try:
            if not self.memory or not self.memory.registers:
                return

            # Try to extract topic from the brief title
            topic = f"Cycle {self.state.cycle_number} investigation"
            for line in (self._final_response or "").split("\n"):
                stripped = line.strip().lstrip("#").strip()
                if stripped.lower().startswith("legba situation brief:"):
                    topic = stripped.split(":", 1)[1].strip() if ":" in stripped else stripped
                    break

            history = await self.memory.registers.get_json(_SYNTH_HISTORY_KEY) or []
            history.insert(0, {
                "cycle": self.state.cycle_number,
                "topic": topic,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            # Keep bounded
            history = history[:_MAX_SYNTH_HISTORY]
            await self.memory.registers.set_json(_SYNTH_HISTORY_KEY, history)
        except Exception as e:
            log.warning("Failed to update synth history: %s", e)
