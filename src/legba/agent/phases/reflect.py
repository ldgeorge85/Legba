"""REFLECT phase — structured extraction from cycle results."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ...shared.schemas.comms import InboxMessage
from ...shared.schemas.memory import Fact

if TYPE_CHECKING:
    from ..cycle import AgentCycle


class ReflectMixin:
    """Phase 5: Evaluate outcomes with structured extraction."""

    async def _reflect(self: AgentCycle) -> None:
        """
        Phase 5: Evaluate outcomes with structured extraction.

        Requests JSON output from the LLM to extract facts, entities,
        relationships, and self-assessment. Parsed results are stored
        via the memory manager.
        """
        self.logger.log_phase("reflect")
        self.state.phase = "reflect"

        # Build reflection context from working memory + final response
        working_memory_text = self.llm.working_memory.full_text()
        results_summary = self._final_response[:3000] if self._final_response else "(no response)"

        inbox_messages = [InboxMessage(**m) for m in self.state.inbox_messages]
        reflect_messages = self.assembler.assemble_reflect_prompt(
            cycle_plan=self._cycle_plan,
            working_memory=working_memory_text,
            results_summary=results_summary,
            seed_goal=self.state.seed_goal,
            cycle_number=self.state.cycle_number,
            inbox_messages=inbox_messages if inbox_messages else None,
        )

        try:
            response = await self.llm.complete(
                reflect_messages,
                purpose="reflect",
            )
            self._reflection = response.content

            # Try to parse structured JSON from the reflection
            self._reflection_data = self._parse_reflection(self._reflection)

            # Store extracted facts
            await self._store_reflection_facts()

            # Store extracted entities and relationships in graph
            await self._store_reflection_graph()

        except Exception as e:
            self._reflection = f"Reflection failed: {e}"
            self._reflection_data = {}
            self.logger.log_error(f"Reflection failed: {e}")

        significance = float(self._reflection_data.get("significance", 0.0))
        self.logger.log("reflect_complete",
                        reflection_length=len(self._reflection),
                        significance=significance,
                        facts_extracted=len(self._reflection_data.get("facts_learned", [])),
                        entities_extracted=len(self._reflection_data.get("entities_discovered", [])))

    def _parse_reflection(self: AgentCycle, text: str) -> dict:
        """Parse structured JSON from the reflection response.

        The model may include chain-of-thought reasoning before the JSON,
        which can contain small JSON snippets like {} or {"key": "val"}.
        We scan for all top-level JSON objects and return the one that
        looks like a real reflection (contains 'cycle_summary').
        """
        pos = 0
        while pos < len(text):
            start = text.find("{", pos)
            if start < 0:
                break
            depth = 0
            for i in range(start, len(text)):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            candidate = json.loads(text[start:i + 1])
                            if isinstance(candidate, dict) and "cycle_summary" in candidate:
                                return candidate
                        except (json.JSONDecodeError, ValueError):
                            pass
                        pos = i + 1
                        break
            else:
                break
        return {}

    async def _store_reflection_facts(self: AgentCycle) -> None:
        """Store facts extracted from the reflection phase."""
        from ..memory.fact_normalize import normalize_fact_predicate, normalize_fact_value

        facts = self._reflection_data.get("facts_learned", [])
        for fact_data in facts:
            try:
                subject = str(fact_data.get("subject", "")).strip()
                predicate = normalize_fact_predicate(str(fact_data.get("predicate", "")))
                value = normalize_fact_value(str(fact_data.get("value", "")))
                if not (subject and predicate and value):
                    continue

                fact = Fact(
                    subject=subject,
                    predicate=predicate,
                    value=value,
                    confidence=min(float(fact_data.get("confidence", 0.5)), 1.0),
                    source_cycle=self.state.cycle_number,
                )

                # Generate embedding for semantic search
                try:
                    embedding = await self.llm.generate_embedding(
                        f"{subject} {predicate} {value}"
                    )
                except Exception:
                    embedding = None

                await self.memory.store_fact(fact, embedding=embedding)

                # Auto-supersede conflicting facts
                try:
                    if fact.subject and fact.predicate:
                        existing = await self.memory.structured.query_facts(
                            subject=fact.subject, predicate=fact.predicate
                        )
                        for old in existing:
                            if (old.id != fact.id
                                    and str(old.value) != str(fact.value)
                                    and not old.superseded_by):
                                await self.memory.structured.supersede_fact(old.id, fact)
                                self.logger.log("fact_auto_superseded",
                                    subject=fact.subject,
                                    predicate=fact.predicate,
                                    old_value=str(old.value)[:100],
                                    new_value=str(fact.value)[:100])
                except Exception:
                    pass  # Best-effort, don't break reflect

            except Exception as e:
                self.logger.log_error(f"Failed to store reflection fact: {e}")

    async def _store_reflection_graph(self: AgentCycle) -> None:
        """Store entities and relationships from reflection in the graph."""
        from ..tools.builtins.graph_tools import normalize_relationship_type, _find_similar_entity
        from ...shared.schemas.memory import Entity

        entities = self._reflection_data.get("entities_discovered", [])
        relationships = self._reflection_data.get("relationships", [])
        name_remap: dict[str, str] = {}

        for entity_data in entities:
            try:
                name = entity_data.get("name", "")
                etype = entity_data.get("type", "Entity")
                if not name:
                    continue
                # Fuzzy dedup: check for similar existing entity
                existing = await self.memory.graph.find_entity(name)
                if not existing:
                    similar = await _find_similar_entity(
                        self.memory.graph, name, etype,
                    )
                    if similar:
                        name_remap[name] = similar
                        name = similar
                props = entity_data.get("properties", {})
                props["discovered_cycle"] = self.state.cycle_number
                entity = Entity(name=name, entity_type=etype, properties=props)
                await self.memory.graph.upsert_entity(entity)
            except Exception as e:
                self.logger.log_error(f"Failed to store graph entity: {e}")

        for rel in relationships:
            try:
                from_e = rel.get("from_entity", "")
                to_e = rel.get("to_entity", "")
                rel_type = rel.get("relationship", "RELATED_TO")
                if not (from_e and to_e):
                    continue
                # Apply name remappings from dedup
                from_e = name_remap.get(from_e, from_e)
                to_e = name_remap.get(to_e, to_e)
                rel_type, _ = normalize_relationship_type(rel_type)
                props = rel.get("properties", {})
                props["discovered_cycle"] = self.state.cycle_number
                await self.memory.graph.add_relationship(from_e, to_e, rel_type, props)
            except Exception as e:
                self.logger.log_error(f"Failed to store graph relationship: {e}")
