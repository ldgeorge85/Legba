"""Adversarial context helper for ANALYSIS phase injection.

Queries recent adversarial flags from signal JSONB data and formats them
for inclusion in the ANALYSIS cycle prompt context. Lightweight — no LLM.

Usage:
    summary = await get_adversarial_summary(pg_pool)
    # Returns formatted string or empty string if no flags.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

import asyncpg

logger = logging.getLogger("legba.shared.adversarial_context")


async def get_adversarial_summary(pool: asyncpg.Pool) -> str:
    """Query recent adversarial flags for injection into ANALYSIS context.

    Scans signals from the last 24 hours for adversarial_flags in their
    JSONB data field. Groups and summarizes flags by type and entity.

    Returns a formatted string like:
        '### Adversarial Flags (last 24h)
        - 3 signals flagged as velocity_spike on entity "Iran"
        - 5 signals flagged as semantic_echo (sources: RT, TASS, Sputnik)
        ...'
    or empty string if no flags found.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    try:
        async with pool.acquire() as conn:
            # Find signals with adversarial flags in the last 24h
            rows = await conn.fetch("""
                SELECT s.id,
                       s.data->'adversarial_flags' AS flags,
                       s.title
                FROM signals s
                WHERE s.created_at > $1
                  AND s.data ? 'adversarial_flags'
                  AND jsonb_array_length(COALESCE(s.data->'adversarial_flags', '[]'::jsonb)) > 0
                ORDER BY s.created_at DESC
                LIMIT 200
            """, cutoff)

            if not rows:
                return ""

            # Aggregate flags by type
            flag_groups: dict[str, list[dict]] = {}
            for row in rows:
                flags = row["flags"]
                if not flags:
                    continue
                # flags is a JSONB array
                if isinstance(flags, str):
                    import json
                    flags = json.loads(flags)
                if not isinstance(flags, list):
                    continue
                for flag in flags:
                    flag_type = flag.get("type", "unknown")
                    group = flag_groups.setdefault(flag_type, [])
                    group.append({
                        "signal_id": str(row["id"]),
                        "title": row["title"],
                        "severity": flag.get("severity", "medium"),
                        "entity_name": flag.get("entity_name", ""),
                        "description": flag.get("description", ""),
                        "provenance_group": flag.get("provenance_group", ""),
                    })

            if not flag_groups:
                return ""

            # Build summary
            lines = ["### Adversarial Flags (last 24h)"]
            total_signals = set()

            for flag_type, flags in sorted(flag_groups.items()):
                signal_ids = {f["signal_id"] for f in flags}
                total_signals.update(signal_ids)

                # Group by entity if available
                entity_counter: Counter = Counter()
                severity_counter: Counter = Counter()
                descriptions: list[str] = []

                for f in flags:
                    if f["entity_name"]:
                        entity_counter[f["entity_name"]] += 1
                    severity_counter[f["severity"]] += 1
                    if f["description"] and f["description"] not in descriptions:
                        descriptions.append(f["description"])

                severity_str = ""
                high_count = severity_counter.get("high", 0)
                if high_count > 0:
                    severity_str = f" ({high_count} HIGH severity)"

                if entity_counter:
                    entity_parts = [
                        f'"{name}" ({count})'
                        for name, count in entity_counter.most_common(5)
                    ]
                    lines.append(
                        f"- **{flag_type}**: {len(signal_ids)} signals — "
                        f"entities: {', '.join(entity_parts)}{severity_str}"
                    )
                else:
                    # Use first description for context
                    desc_part = ""
                    if descriptions:
                        desc_part = f" — {descriptions[0][:100]}"
                    lines.append(
                        f"- **{flag_type}**: {len(signal_ids)} signals"
                        f"{desc_part}{severity_str}"
                    )

            lines.append(f"\nTotal: {len(total_signals)} unique signals flagged")

            return "\n".join(lines)

    except Exception as e:
        logger.debug("Adversarial summary query failed: %s", e)
        return ""
