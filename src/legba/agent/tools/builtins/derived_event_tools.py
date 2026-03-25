"""
Derived Event Management Tools

Tools for the agent to create, update, query, and link derived events.
Derived events are real-world occurrences extracted from signals. The
ingestion clusterer creates auto-events; these tools let the agent
create agent-events and refine both.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import UUID

logger = logging.getLogger(__name__)

from ....shared.schemas.tools import ToolDefinition, ToolParameter


# ---------------------------------------------------------------------------
# Duplicate detection helpers (mirrored from event_tools.py)
# ---------------------------------------------------------------------------

_STOP = {"a", "an", "the", "of", "in", "on", "at", "to", "for", "and", "or",
         "is", "was", "by", "from", "with"}


def _title_words(title: str) -> set[str]:
    return {w for w in title.lower().split() if w not in _STOP and len(w) > 1}


def _title_similarity(a: str, b: str) -> float:
    wa, wb = _title_words(a), _title_words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)

if TYPE_CHECKING:
    from ...memory.structured import StructuredStore
    from ...tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

DERIVED_EVENT_CREATE_DEF = ToolDefinition(
    name="event_create",
    description="Create a new derived event (real-world occurrence) from your analysis. "
                "Use this when you identify a significant real-world event from signals "
                "that the automatic clusterer missed. Set source_method to 'agent'.",
    parameters=[
        ToolParameter(name="title", type="string",
                      description="Clear, concise event title"),
        ToolParameter(name="summary", type="string",
                      description="Brief summary of what happened",
                      required=False),
        ToolParameter(name="category", type="string",
                      description="Category: conflict, political, economic, technology, "
                                  "health, environment, social, disaster, other",
                      required=False),
        ToolParameter(name="event_type", type="string",
                      description="Type: incident (discrete), development (ongoing), "
                                  "shift (state change), threshold (metric crossing). Default: incident",
                      required=False),
        ToolParameter(name="severity", type="string",
                      description="Severity: critical, high, medium, low, routine. Default: medium",
                      required=False),
        ToolParameter(name="time_start", type="string",
                      description="When the event started (ISO 8601)",
                      required=False),
        ToolParameter(name="time_end", type="string",
                      description="When the event ended or last updated (ISO 8601). "
                                  "Omit for ongoing events.",
                      required=False),
        ToolParameter(name="actors", type="string",
                      description="Comma-separated actor names (people, orgs, countries)",
                      required=False),
        ToolParameter(name="locations", type="string",
                      description="Comma-separated location names",
                      required=False),
        ToolParameter(name="tags", type="string",
                      description="Comma-separated tags",
                      required=False),
        ToolParameter(name="signal_ids", type="string",
                      description="Comma-separated signal UUIDs to link as evidence",
                      required=False),
    ],
)

DERIVED_EVENT_UPDATE_DEF = ToolDefinition(
    name="event_update",
    description="Update an existing derived event. Use to refine auto-created events: "
                "improve titles, adjust severity, merge events, add summary, "
                "or advance lifecycle status.",
    parameters=[
        ToolParameter(name="event_id", type="string",
                      description="UUID of the event to update"),
        ToolParameter(name="title", type="string",
                      description="New title (leave empty to keep current)",
                      required=False),
        ToolParameter(name="summary", type="string",
                      description="New summary",
                      required=False),
        ToolParameter(name="severity", type="string",
                      description="New severity: critical, high, medium, low, routine",
                      required=False),
        ToolParameter(name="event_type", type="string",
                      description="New type: incident, development, shift, threshold",
                      required=False),
        ToolParameter(name="category", type="string",
                      description="New category",
                      required=False),
        ToolParameter(name="lifecycle_status", type="string",
                      description="Lifecycle status: emerging, active, evolving, stable, historical",
                      required=False),
    ],
)

DERIVED_EVENT_QUERY_DEF = ToolDefinition(
    name="event_query",
    description="Query derived events (real-world occurrences) with filters. "
                "These are curated events, not raw signals. Returns events "
                "sorted by time_start descending.",
    parameters=[
        ToolParameter(name="category", type="string",
                      description="Filter by category",
                      required=False),
        ToolParameter(name="severity", type="string",
                      description="Filter by severity (critical, high, medium, low, routine)",
                      required=False),
        ToolParameter(name="event_type", type="string",
                      description="Filter by type (incident, development, shift, threshold)",
                      required=False),
        ToolParameter(name="since", type="string",
                      description="Events after this ISO 8601 timestamp",
                      required=False),
        ToolParameter(name="until", type="string",
                      description="Events before this ISO 8601 timestamp",
                      required=False),
        ToolParameter(name="min_signals", type="number",
                      description="Minimum signal count (filter for well-evidenced events)",
                      required=False),
        ToolParameter(name="source_method", type="string",
                      description="Filter by source: auto, agent, manual",
                      required=False),
        ToolParameter(name="limit", type="number",
                      description="Max results (default: 20)",
                      required=False),
    ],
)

DERIVED_EVENT_LINK_SIGNAL_DEF = ToolDefinition(
    name="event_link_signal",
    description="Link a signal to a derived event as evidence. Use when you find "
                "a signal that belongs to an existing event but wasn't caught by "
                "the automatic clusterer.",
    parameters=[
        ToolParameter(name="event_id", type="string",
                      description="UUID of the derived event"),
        ToolParameter(name="signal_id", type="string",
                      description="UUID of the signal to link"),
        ToolParameter(name="relevance", type="number",
                      description="Relevance score 0.0-1.0 (default: 1.0)",
                      required=False),
    ],
)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register(
    registry: ToolRegistry,
    *,
    structured: StructuredStore,
) -> None:
    """Register derived event tools with the given registry."""

    def _check_pg() -> str | None:
        if structured is None or not structured._available:
            return "Error: Structured store (Postgres) is not available"
        return None

    async def event_create_handler(args: dict) -> str:
        pg_err = _check_pg()
        if pg_err:
            return pg_err

        title = args.get("title", "").strip()
        if not title:
            return "Error: title is required"

        # --- Dedup: check for existing events with similar titles (last 7 days) ---
        try:
            async with structured._pool.acquire() as conn:
                # Exact title match
                exact = await conn.fetchrow(
                    "SELECT id, title FROM events "
                    "WHERE lower(title) = $1 AND created_at >= NOW() - INTERVAL '7 days'",
                    title.lower(),
                )
                if exact:
                    return json.dumps({
                        "status": "duplicate_detected",
                        "existing_event_id": str(exact["id"]),
                        "existing_title": exact["title"],
                        "reason": "exact title match — use event_update to modify or event_link_signal to add evidence",
                    }, indent=2)

                # Jaccard similarity check
                threshold = 0.4 if len(title.split()) <= 5 else 0.5
                candidates = await conn.fetch(
                    "SELECT id, title FROM events "
                    "WHERE created_at >= NOW() - INTERVAL '7 days' "
                    "ORDER BY created_at DESC LIMIT 300",
                )
                for cand in candidates:
                    sim = _title_similarity(title, cand["title"])
                    if sim >= threshold:
                        return json.dumps({
                            "status": "duplicate_detected",
                            "existing_event_id": str(cand["id"]),
                            "existing_title": cand["title"],
                            "similarity": round(sim, 3),
                            "reason": f"similar event exists (Jaccard {sim:.2f}) — use event_update or event_link_signal instead",
                        }, indent=2)
        except Exception as e:
            logger.debug("Event dedup check failed: %s", e)

        from ....shared.schemas.derived_events import (
            DerivedEvent, EventType, EventSeverity,
        )
        from ....shared.schemas.signals import SignalCategory

        # Parse category
        cat_str = args.get("category", "other").lower().strip()
        try:
            category = SignalCategory(cat_str)
        except ValueError:
            category = SignalCategory.OTHER

        # Parse event_type
        type_str = args.get("event_type", "incident").lower().strip()
        try:
            event_type = EventType(type_str)
        except ValueError:
            event_type = EventType.INCIDENT

        # Parse severity
        sev_str = args.get("severity", "medium").lower().strip()
        try:
            severity = EventSeverity(sev_str)
        except ValueError:
            severity = EventSeverity.MEDIUM

        # Parse timestamps
        time_start = None
        time_end = None
        if args.get("time_start"):
            try:
                time_start = datetime.fromisoformat(args["time_start"].replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pass
        if args.get("time_end"):
            try:
                time_end = datetime.fromisoformat(args["time_end"].replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pass

        # Parse actors/locations/tags
        actors = [a.strip() for a in args.get("actors", "").split(",") if a.strip()]
        locations = [loc.strip() for loc in args.get("locations", "").split(",") if loc.strip()]
        tags = [t.strip() for t in args.get("tags", "").split(",") if t.strip()]

        event = DerivedEvent(
            title=title,
            summary=args.get("summary", ""),
            category=category,
            event_type=event_type,
            severity=severity,
            time_start=time_start,
            time_end=time_end,
            actors=actors,
            locations=locations,
            tags=tags,
            confidence=0.7,  # Agent-created events start at 0.7
            signal_count=0,
            source_method="agent",
        )

        ok = await structured.save_derived_event(event)
        if not ok:
            return "Error: failed to save derived event"

        # Link signals if provided
        linked = 0
        signal_ids_str = args.get("signal_ids", "")
        if signal_ids_str:
            for sid in signal_ids_str.split(","):
                sid = sid.strip()
                if sid:
                    try:
                        await structured.link_signal_to_event(UUID(sid), event.id)
                        linked += 1
                    except Exception:
                        pass
            event.signal_count = linked
            if linked:
                await structured.save_derived_event(event)

        result = {
            "status": "created",
            "event_id": str(event.id),
            "title": title,
            "severity": severity.value,
            "event_type": event_type.value,
            "signals_linked": linked,
        }
        return json.dumps(result, indent=2)

    async def event_update_handler(args: dict) -> str:
        pg_err = _check_pg()
        if pg_err:
            return pg_err

        event_id_str = args.get("event_id", "").strip()
        if not event_id_str:
            return "Error: event_id is required"

        try:
            event_id = UUID(event_id_str)
        except ValueError:
            return "Error: invalid event_id UUID"

        # Fetch existing
        from ....shared.schemas.derived_events import DerivedEvent, EventType, EventSeverity
        from ....shared.schemas.signals import SignalCategory

        rows = await structured._pool.fetch(
            "SELECT data FROM events WHERE id = $1", event_id,
        )
        if not rows:
            return f"Error: event {event_id_str[:8]} not found"

        event = DerivedEvent.model_validate_json(rows[0]["data"])

        # Apply updates
        if args.get("title"):
            event.title = args["title"].strip()
        if args.get("summary"):
            event.summary = args["summary"].strip()
        if args.get("severity"):
            try:
                event.severity = EventSeverity(args["severity"].lower().strip())
            except ValueError:
                pass
        if args.get("event_type"):
            try:
                event.event_type = EventType(args["event_type"].lower().strip())
            except ValueError:
                pass
        if args.get("category"):
            try:
                event.category = SignalCategory(args["category"].lower().strip())
            except ValueError:
                pass

        # Lifecycle status update
        lifecycle_status = None
        _VALID_LIFECYCLE = {"emerging", "active", "evolving", "stable", "historical"}
        if args.get("lifecycle_status"):
            ls = args["lifecycle_status"].lower().strip()
            if ls in _VALID_LIFECYCLE:
                lifecycle_status = ls
            else:
                return f"Error: Invalid lifecycle_status '{ls}'. Use: {', '.join(sorted(_VALID_LIFECYCLE))}"

        event.updated_at = datetime.now(timezone.utc)
        ok = await structured.save_derived_event(event, lifecycle_status=lifecycle_status)
        if not ok:
            return "Error: failed to update event"

        # If lifecycle changed, update lifecycle_changed_at directly
        if lifecycle_status:
            try:
                await structured._pool.execute(
                    "UPDATE events SET lifecycle_status = $1, lifecycle_changed_at = NOW() WHERE id = $2",
                    lifecycle_status, event_id,
                )
            except Exception:
                pass  # Column may not exist yet

        result = {
            "status": "updated",
            "event_id": event_id_str[:8],
            "title": event.title,
            "severity": event.severity.value,
        }
        if lifecycle_status:
            result["lifecycle_status"] = lifecycle_status
        return json.dumps(result, indent=2)

    async def event_query_handler(args: dict) -> str:
        pg_err = _check_pg()
        if pg_err:
            return pg_err

        limit = min(int(args.get("limit", 20)), 50)

        # Parse since/until
        since = None
        until = None
        if args.get("since"):
            try:
                since = datetime.fromisoformat(args["since"].replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pass
        if args.get("until"):
            try:
                until = datetime.fromisoformat(args["until"].replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pass

        min_signals = None
        if args.get("min_signals"):
            try:
                min_signals = int(args["min_signals"])
            except (ValueError, TypeError):
                pass

        events = await structured.query_derived_events(
            category=args.get("category"),
            event_type=args.get("event_type"),
            severity=args.get("severity"),
            since=since,
            until=until,
            min_signal_count=min_signals,
            source_method=args.get("source_method"),
            limit=limit,
        )

        if not events:
            return "No derived events found matching filters."

        results = []
        for ev in events:
            results.append({
                "id": str(ev.id)[:8],
                "title": ev.title,
                "category": str(ev.category.value if hasattr(ev.category, 'value') else ev.category),
                "event_type": ev.event_type.value,
                "severity": ev.severity.value,
                "signal_count": ev.signal_count,
                "source_method": ev.source_method,
                "time_start": ev.time_start.isoformat() if ev.time_start else None,
                "time_end": ev.time_end.isoformat() if ev.time_end else None,
                "actors": ev.actors[:5],
                "locations": ev.locations[:5],
                "confidence": ev.confidence,
            })

        return json.dumps({
            "count": len(results),
            "events": results,
        }, indent=2)

    async def event_link_signal_handler(args: dict) -> str:
        pg_err = _check_pg()
        if pg_err:
            return pg_err

        event_id_str = args.get("event_id", "").strip()
        signal_id_str = args.get("signal_id", "").strip()
        if not event_id_str or not signal_id_str:
            return "Error: event_id and signal_id are required"

        try:
            event_id = UUID(event_id_str)
            signal_id = UUID(signal_id_str)
        except ValueError:
            return (
                f"Error: invalid UUID (event_id='{event_id_str}', signal_id='{signal_id_str}'). "
                "You must use the actual UUID returned by event_create, not a placeholder. "
                "Call event_create first, then use the returned event_id to link signals."
            )

        relevance = float(args.get("relevance", 1.0))
        ok = await structured.link_signal_to_event(signal_id, event_id, relevance)
        if not ok:
            return "Error: failed to link signal to event"

        # Update signal_count on the event
        try:
            await structured._pool.execute(
                """
                UPDATE events SET
                    signal_count = (SELECT COUNT(*) FROM signal_event_links WHERE event_id = $1),
                    updated_at = NOW()
                WHERE id = $1
                """,
                event_id,
            )
        except Exception:
            pass

        return json.dumps({
            "status": "linked",
            "event_id": event_id_str[:8],
            "signal_id": signal_id_str[:8],
            "relevance": relevance,
        })

    # Register all tools
    registry.register(DERIVED_EVENT_CREATE_DEF, event_create_handler)
    registry.register(DERIVED_EVENT_UPDATE_DEF, event_update_handler)
    registry.register(DERIVED_EVENT_QUERY_DEF, event_query_handler)
    registry.register(DERIVED_EVENT_LINK_SIGNAL_DEF, event_link_signal_handler)
