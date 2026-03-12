"""NARRATE phase — journal entries, consolidation, OpenSearch archiving."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ...shared.schemas.comms import InboxMessage

if TYPE_CHECKING:
    from ..cycle import AgentCycle


class NarrateMixin:
    """Narrate phase: journal entries and consolidation."""

    _JOURNAL_KEY = "journal"
    _JOURNAL_MAX_ENTRIES = 30
    _JOURNAL_INDEX = "legba-journal"

    async def _narrate(self: AgentCycle) -> None:
        """Write 1-3 journal entries reflecting on this cycle."""
        self.logger.log_phase("narrate")
        try:
            cycle_summary = self._reflection_data.get(
                "cycle_summary",
                self._final_response[:500] if self._final_response else "empty cycle",
            )

            inbox_messages = [InboxMessage(**m) for m in self.state.inbox_messages]
            narrate_messages = self.assembler.assemble_narrate_prompt(
                cycle_summary=cycle_summary,
                journal_context=self._journal_context,
                inbox_messages=inbox_messages if inbox_messages else None,
            )

            response = await self.llm.complete(
                narrate_messages,
                purpose="narrate",
            )

            # Parse JSON array of strings
            raw = response.content.strip()
            # Find the JSON array in the response
            start = raw.find("[")
            end = raw.rfind("]")
            if start >= 0 and end > start:
                entries = json.loads(raw[start:end + 1])
                if isinstance(entries, list):
                    entries = [str(e) for e in entries if e][:3]
                else:
                    entries = []
            else:
                entries = []

            if entries:
                await self._store_journal_entries(entries)
                await self._extract_journal_leads(entries)
                self.logger.log("narrate_complete", entries=len(entries))
            else:
                self.logger.log("narrate_complete", entries=0)

        except Exception as e:
            self.logger.log_error(f"Narrate failed: {e}")

    async def _extract_journal_leads(self: AgentCycle, entries: list[str]) -> None:
        """Extract investigation leads from journal entries and store in Redis.

        Scans entries for signal words indicating areas worth investigating,
        and feeds them back into the next cycle's planning context.
        """
        signal_words = {
            "investigate", "unclear", "surprising", "unexpected", "contradicts",
            "need to", "should look", "worth exploring", "follow up",
            "question", "puzzling", "anomaly", "gap", "missing",
            "why", "how does", "what if", "wonder",
        }
        leads = []
        for entry in entries:
            entry_lower = entry.lower()
            if any(w in entry_lower for w in signal_words):
                # Trim to a reasonable lead length
                lead = entry[:200].strip()
                if lead:
                    leads.append(lead)

        if leads and self.memory and self.memory.registers:
            try:
                # Keep max 10 leads, newest first
                existing = await self.memory.registers.get_json("journal_leads") or []
                combined = leads + existing
                await self.memory.registers.set_json("journal_leads", combined[:10])
            except Exception:
                pass

    async def _store_journal_entries(self: AgentCycle, entries: list[str]) -> None:
        """Append journal entries to Redis storage and archive to OpenSearch."""
        if not self.memory or not self.memory.registers:
            return

        ts = datetime.now(timezone.utc).isoformat()

        journal_data = await self.memory.registers.get_json(self._JOURNAL_KEY) or {}
        raw_entries = journal_data.get("entries", [])
        raw_entries.append({
            "cycle": self.state.cycle_number,
            "timestamp": ts,
            "entries": entries,
        })

        # Trim old entries (keep last N)
        if len(raw_entries) > self._JOURNAL_MAX_ENTRIES:
            raw_entries = raw_entries[-self._JOURNAL_MAX_ENTRIES:]

        journal_data["entries"] = raw_entries
        await self.memory.registers.set_json(self._JOURNAL_KEY, journal_data)

        # Archive to OpenSearch for permanent record
        await self._archive_journal_to_opensearch(
            doc_type="entry",
            cycle=self.state.cycle_number,
            timestamp=ts,
            content="\n".join(entries),
            entries=entries,
        )

    async def _journal_consolidation(self: AgentCycle) -> None:
        """Consolidate recent journal entries into a narrative (introspection only)."""
        self.logger.log_phase("journal_consolidation")
        try:
            journal_data = await self.memory.registers.get_json(self._JOURNAL_KEY) or {}
            raw_entries = journal_data.get("entries", [])
            previous_consolidation = journal_data.get("consolidation", "")

            if not raw_entries:
                self.logger.log("journal_consolidation_skipped", reason="no entries")
                return

            # Format entries for the consolidation prompt
            entry_lines = []
            for e in raw_entries:
                cycle_n = e.get("cycle", "?")
                for line in e.get("entries", []):
                    entry_lines.append(f"[cycle {cycle_n}] {line}")

            entries_text = "\n".join(entry_lines)

            consolidation_messages = self.assembler.assemble_journal_consolidation_prompt(
                entries=entries_text,
                previous_consolidation=previous_consolidation,
            )

            response = await self.llm.complete(
                consolidation_messages,
                purpose="journal_consolidation",
            )

            new_consolidation = response.content.strip()
            # Clean any model artifacts
            for token in ["<|end|>", "<|return|>"]:
                new_consolidation = new_consolidation.replace(token, "")

            # Archive consolidation to OpenSearch before clearing entries
            consolidation_ts = datetime.now(timezone.utc).isoformat()
            await self._archive_journal_to_opensearch(
                doc_type="consolidation",
                cycle=self.state.cycle_number,
                timestamp=consolidation_ts,
                content=new_consolidation,
                entries_consolidated=len(raw_entries),
                source_cycles=[e.get("cycle") for e in raw_entries if e.get("cycle")],
            )

            # Store consolidation, clear old entries
            journal_data["consolidation"] = new_consolidation
            journal_data["consolidation_cycle"] = self.state.cycle_number
            journal_data["consolidation_timestamp"] = consolidation_ts
            journal_data["entries"] = []  # Clear raw entries after consolidation
            await self.memory.registers.set_json(self._JOURNAL_KEY, journal_data)

            self.logger.log("journal_consolidation_complete",
                            entries_consolidated=len(raw_entries),
                            consolidation_length=len(new_consolidation))

        except Exception as e:
            self.logger.log_error(f"Journal consolidation failed: {e}")

    async def _archive_journal_to_opensearch(self: AgentCycle, *, doc_type: str, cycle: int,
                                              timestamp: str, content: str,
                                              **extra) -> None:
        """Archive a journal document to OpenSearch for permanent storage."""
        if not self.opensearch or not self.opensearch.available:
            return
        try:
            doc = {
                "type": doc_type,
                "cycle": cycle,
                "cycle_number": cycle,
                "timestamp": timestamp,
                "content": content,
                **extra,
            }
            await self.opensearch.index_document(
                index=self._JOURNAL_INDEX,
                document=doc,
                doc_id=f"{doc_type}-{cycle}",
            )
        except Exception:
            pass  # Best-effort — don't break the cycle if archiving fails
