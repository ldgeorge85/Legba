"""REFLECT phase — structured extraction from cycle results."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ...shared.schemas.comms import InboxMessage

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

            # Store extracted entities and relationships in graph
            await self._store_reflection_graph()

        except Exception as e:
            self._reflection = f"Reflection failed: {e}"
            self._reflection_data = {}
            self.logger.log_error(f"Reflection failed: {e}")

        # Graph quality checks — warn on vague defaults so we can track frequency
        entities = self._reflection_data.get("entities_discovered", [])
        relationships = self._reflection_data.get("relationships", [])
        if entities:
            unknown_types = [e for e in entities if e.get("type", "").lower() == "unknown"]
            if unknown_types:
                self.logger.log_error(
                    f"Graph quality: {len(unknown_types)} entities with 'Unknown' type — "
                    f"names: {[e.get('name', '?') for e in unknown_types[:5]]}"
                )
        if relationships:
            vague_rels = [r for r in relationships if r.get("relationship", "").lower() in ("relatedto", "related_to")]
            if vague_rels:
                self.logger.log_error(
                    f"Graph quality: {len(vague_rels)} relationships using 'RelatedTo' — "
                    f"pairs: {[(r.get('from_entity', '?'), r.get('to_entity', '?')) for r in vague_rels[:5]]}"
                )

        significance = float(self._reflection_data.get("significance", 0.0))
        self.logger.log("reflect_complete",
                        reflection_length=len(self._reflection),
                        significance=significance,
                        facts_extracted=len(self._reflection_data.get("facts_learned", [])),
                        entities_extracted=len(entities))

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
