"""
Entity Intelligence Tools — entity_profile, entity_inspect, entity_resolve

Manage structured entity profiles (the persistent world model). Profiles
accumulate sourced assertions over time, are versioned, and link to events.
Entity resolution maps string names to canonical entities.

Profiles live in Postgres (entity_profiles table, JSONB). The AGE knowledge
graph holds relationship topology only — entity_profile syncs vertices
to AGE on create/update for graph traversal.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import UUID

from ....shared.schemas.tools import ToolDefinition, ToolParameter

if TYPE_CHECKING:
    from ...memory.graph import GraphStore
    from ...memory.structured import StructuredStore
    from ..registry import ToolRegistry


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

ENTITY_PROFILE_DEF = ToolDefinition(
    name="entity_profile",
    description="Create or update a structured entity profile with sourced assertions. "
                "Resolves the name first (canonical -> alias -> fuzzy -> new). "
                "Merges assertions into the specified section, superseding same-key "
                "assertions when new confidence is higher. Bumps version automatically.",
    parameters=[
        ToolParameter(name="entity_name", type="string",
                      description="Name of the entity (e.g. 'Russia', 'Vladimir Putin', 'NATO')"),
        ToolParameter(name="entity_type", type="string",
                      description="Type: country, organization, person, location, military_unit, "
                                  "political_party, armed_group, international_org, corporation, "
                                  "media_outlet, event_series, concept, commodity, infrastructure, other",
                      required=False),
        ToolParameter(name="section", type="string",
                      description="Profile section to add assertions to (e.g. 'government', "
                                  "'military', 'identity', 'economy'). Default: 'general'",
                      required=False),
        ToolParameter(name="assertions", type="string",
                      description='MUST be a valid JSON array string. Example: '
                                  '"[{\\"key\\": \\"president\\", \\"value\\": \\"Name\\", \\"confidence\\": 0.95}]". '
                                  'Each object needs "key" and "value". "confidence" optional (default 0.7). '
                                  'The value must parse as a JSON array, not a plain string or object.',
                      required=False),
        ToolParameter(name="summary", type="string",
                      description="One-paragraph summary of the entity (overwrites existing)",
                      required=False),
        ToolParameter(name="aliases", type="string",
                      description="Comma-separated alternative names (added to existing aliases)",
                      required=False),
        ToolParameter(name="source_event_id", type="string",
                      description="UUID of the source event for all assertions in this call",
                      required=False),
        ToolParameter(name="tags", type="string",
                      description="Comma-separated tags for filtering and context (e.g. 'nato-member,nuclear-power,g7'). Added to existing tags.",
                      required=False),
    ],
)

ENTITY_INSPECT_DEF = ToolDefinition(
    name="entity_inspect",
    description="Read an entity profile with completeness score, staleness, linked events, "
                "and version history. Supports historical view via as_of parameter.",
    parameters=[
        ToolParameter(name="entity_name", type="string",
                      description="Name of the entity to inspect"),
        ToolParameter(name="as_of", type="string",
                      description="ISO 8601 timestamp for historical view (e.g. '2026-03-01T00:00:00Z'). "
                                  "Shows profile as it was at that point in time.",
                      required=False),
        ToolParameter(name="include_events", type="string",
                      description="Set to 'true' to include linked events (default: false)",
                      required=False),
        ToolParameter(name="include_history", type="string",
                      description="Set to 'true' to include version history (default: false)",
                      required=False),
    ],
)

ENTITY_RESOLVE_DEF = ToolDefinition(
    name="entity_resolve",
    description="Resolve a string name to a canonical entity profile. Creates a stub profile "
                "if no match found. Optionally links the entity to an event with a role.",
    parameters=[
        ToolParameter(name="name", type="string",
                      description="Name to resolve (e.g. 'Putin', 'Russian Federation', 'Moscow')"),
        ToolParameter(name="entity_type", type="string",
                      description="Entity type hint for new stubs: country, person, organization, etc.",
                      required=False),
        ToolParameter(name="event_id", type="string",
                      description="UUID of an event to link this entity to",
                      required=False),
        ToolParameter(name="role", type="string",
                      description="Role in the event: actor, location, target, mentioned (default: mentioned)",
                      required=False),
    ],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _str_arg(args: dict, key: str, default: str = "") -> str:
    """Safely get a string argument, handling None and non-string values."""
    val = args.get(key)
    if val is None:
        return default
    if not isinstance(val, str):
        return str(val)
    return val


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register(
    registry: ToolRegistry,
    *,
    structured: StructuredStore,
    graph: GraphStore,
) -> None:
    """Register entity tools wired to structured store and graph."""

    def _check() -> str | None:
        if structured is None or not structured._available:
            return "Error: Structured store (Postgres) is not available"
        return None

    async def entity_profile_handler(args: dict) -> str:
        err = _check()
        if err:
            return err

        from ....shared.schemas.entity_profiles import (
            EntityProfile, EntityType, Assertion,
        )

        entity_name = _str_arg(args, "entity_name").strip()
        if not entity_name:
            return "Error: entity_name is required"

        # Resolve entity type
        etype_str = _str_arg(args, "entity_type", "other")
        try:
            etype = EntityType(etype_str)
        except ValueError:
            etype = EntityType.OTHER

        # Resolve existing profile
        profile = await structured.resolve_entity_name(entity_name)
        is_new = profile is None

        if is_new:
            profile = EntityProfile(
                canonical_name=entity_name,
                entity_type=etype,
            )
        elif etype_str and etype_str != "other":
            # Update type if explicitly provided (don't downgrade to 'other')
            profile.entity_type = etype

        # Summary
        summary = _str_arg(args, "summary").strip()
        if summary:
            profile.summary = summary

        # Aliases
        aliases_str = _str_arg(args, "aliases").strip()
        if aliases_str:
            new_aliases = [a.strip() for a in aliases_str.split(",") if a.strip()]
            existing_lower = {a.lower() for a in profile.aliases}
            for alias in new_aliases:
                if alias.lower() not in existing_lower and alias.lower() != profile.canonical_name.lower():
                    profile.aliases.append(alias)
                    existing_lower.add(alias.lower())

        # Source event ID for assertions
        source_event_id = None
        if args.get("source_event_id"):
            try:
                source_event_id = UUID(str(args["source_event_id"]))
            except ValueError:
                pass

        # Assertions — accept both JSON string and already-parsed list
        section = _str_arg(args, "section", "general").strip()
        raw_assertions_input = args.get("assertions")
        assertions_added = 0
        assertions_superseded = 0
        raw_assertions = None

        if raw_assertions_input:
            if isinstance(raw_assertions_input, list):
                # LLM sent assertions as a parsed list directly
                raw_assertions = raw_assertions_input
            elif isinstance(raw_assertions_input, str):
                assertions_str = raw_assertions_input.strip()
                if assertions_str:
                    try:
                        raw_assertions = json.loads(assertions_str)
                        if not isinstance(raw_assertions, list):
                            raw_assertions = [raw_assertions]
                    except (json.JSONDecodeError, TypeError):
                        return (f"Error: assertions must be a valid JSON array string. "
                                f"Got: {assertions_str[:200]}. "
                                f'Expected format: [{{"key": "name", "value": "val", "confidence": 0.9}}]')

        if raw_assertions:
            if section not in profile.sections:
                profile.sections[section] = []

            existing_assertions = profile.sections[section]

            for raw in raw_assertions:
                if not isinstance(raw, dict) or "key" not in raw or "value" not in raw:
                    continue

                new_assertion = Assertion(
                    key=raw["key"],
                    value=str(raw["value"]),
                    confidence=float(raw.get("confidence", 0.7)),
                    source_event_id=source_event_id,
                    source_url=raw.get("source_url", ""),
                )

                # Supersede same-key assertions if new confidence is higher
                for existing in existing_assertions:
                    if existing.key == raw["key"] and not existing.superseded:
                        if new_assertion.confidence >= existing.confidence:
                            existing.superseded = True
                            assertions_superseded += 1

                existing_assertions.append(new_assertion)
                assertions_added += 1

        # Tags
        tags_str = _str_arg(args, "tags").strip()
        if tags_str:
            new_tags = [t.strip().lower() for t in tags_str.split(",") if t.strip()]
            existing_tags = set(getattr(profile, 'tags', []) or [])
            for tag in new_tags:
                if tag not in existing_tags:
                    profile.tags.append(tag)
                    existing_tags.add(tag)

        # Save
        saved = await structured.save_entity_profile(profile)
        if not saved:
            return f"Error: Failed to save profile for '{entity_name}'"

        # Sync to AGE vertex
        try:
            from ....shared.schemas.memory import Entity
            age_entity = Entity(
                name=profile.canonical_name,
                entity_type=profile.entity_type.value,
                properties={
                    "profile_id": str(profile.id),
                    "completeness": profile.completeness_score,
                    "aliases": ", ".join(profile.aliases) if profile.aliases else "",
                },
            )
            await graph.upsert_entity(age_entity)
        except Exception:
            pass  # AGE sync is best-effort

        result = {
            "status": "created" if is_new else "updated",
            "entity_id": str(profile.id),
            "canonical_name": profile.canonical_name,
            "entity_type": profile.entity_type.value,
            "version": profile.version,
            "completeness": profile.completeness_score,
            "assertions_added": assertions_added,
            "assertions_superseded": assertions_superseded,
            "alias_count": len(profile.aliases),
            "tags": getattr(profile, 'tags', []),
        }
        return json.dumps(result, indent=2)

    async def entity_inspect_handler(args: dict) -> str:
        err = _check()
        if err:
            return err

        entity_name = _str_arg(args, "entity_name").strip()
        if not entity_name:
            return "Error: entity_name is required"

        # Check for historical view
        as_of_str = _str_arg(args, "as_of").strip()
        include_events = _str_arg(args, "include_events").lower() == "true"
        include_history = _str_arg(args, "include_history").lower() == "true"

        # Resolve the entity
        profile = await structured.resolve_entity_name(entity_name)
        if not profile:
            return f"No entity profile found for '{entity_name}'. Use entity_profile to create one."

        # Historical view if requested
        if as_of_str:
            try:
                as_of = datetime.fromisoformat(as_of_str.replace("Z", "+00:00"))
            except ValueError:
                return "Error: Invalid as_of format (use ISO 8601)"

            version_data = await structured.get_entity_profile_version(
                profile.id, as_of=as_of,
            )
            if not version_data:
                return f"No version found for '{entity_name}' as of {as_of_str}"

            result = {
                "historical_view": True,
                "as_of": as_of_str,
                "version": version_data["version"],
                "cycle_number": version_data["cycle_number"],
                "snapshot_date": version_data["created_at"],
                "profile": version_data["data"],
            }
            return json.dumps(result, indent=2, default=str)

        # Current view
        now = datetime.now(timezone.utc)
        staleness_days = (now - profile.updated_at).total_seconds() / 86400.0

        result: dict = {
            "canonical_name": profile.canonical_name,
            "entity_type": profile.entity_type.value,
            "entity_id": str(profile.id),
            "aliases": profile.aliases,
            "summary": profile.summary,
            "version": profile.version,
            "completeness": profile.completeness_score,
            "staleness_days": round(staleness_days, 1),
            "event_link_count": profile.event_link_count,
            "created_at": profile.created_at.isoformat(),
            "updated_at": profile.updated_at.isoformat(),
        }

        # Format sections with assertions
        sections: dict = {}
        for section_name, assertions in profile.sections.items():
            section_data = []
            for a in assertions:
                entry = {
                    "key": a.key,
                    "value": a.value,
                    "confidence": a.confidence,
                }
                if a.superseded:
                    entry["superseded"] = True
                if a.source_event_id:
                    entry["source_event_id"] = str(a.source_event_id)
                if a.source_url:
                    entry["source_url"] = a.source_url
                section_data.append(entry)
            sections[section_name] = section_data
        result["sections"] = sections

        # Linked events
        if include_events:
            events = await structured.get_entity_events(profile.id, limit=10)
            result["linked_events"] = [
                {
                    "title": ev["event"].get("title", ""),
                    "category": ev["event"].get("category", ""),
                    "event_timestamp": ev["event"].get("event_timestamp"),
                    "role": ev["role"],
                    "confidence": ev["confidence"],
                }
                for ev in events
            ]

        # Version history
        if include_history:
            history = []
            for v in range(max(1, profile.version - 4), profile.version + 1):
                vdata = await structured.get_entity_profile_version(
                    profile.id, version=v,
                )
                if vdata:
                    history.append({
                        "version": vdata["version"],
                        "cycle_number": vdata["cycle_number"],
                        "created_at": vdata["created_at"],
                    })
            result["version_history"] = history

        return json.dumps(result, indent=2, default=str)

    async def entity_resolve_handler(args: dict) -> str:
        err = _check()
        if err:
            return err

        from ....shared.schemas.entity_profiles import (
            EntityProfile, EntityType, EventEntityLink,
        )

        name = _str_arg(args, "name").strip()
        if not name:
            return "Error: name is required"

        etype_str = _str_arg(args, "entity_type", "other")
        try:
            etype = EntityType(etype_str)
        except ValueError:
            etype = EntityType.OTHER

        # Try to resolve
        profile = await structured.resolve_entity_name(name)
        resolution = "existing"

        if not profile:
            # Create stub
            profile = EntityProfile(
                canonical_name=name,
                entity_type=etype,
                summary="",
                completeness_score=0.0,
            )
            saved = await structured.save_entity_profile(profile)
            if not saved:
                return f"Error: Failed to create stub profile for '{name}'"
            resolution = "new_stub"

            # Sync stub to AGE
            try:
                from ....shared.schemas.memory import Entity
                age_entity = Entity(
                    name=name,
                    entity_type=etype.value,
                    properties={"profile_id": str(profile.id), "completeness": 0.0},
                )
                await graph.upsert_entity(age_entity)
            except Exception:
                pass

        # Link to event if provided
        event_linked = False
        event_id_str = _str_arg(args, "event_id").strip()
        if event_id_str:
            try:
                event_id = UUID(event_id_str)
                role = _str_arg(args, "role", "mentioned").strip()
                if role not in ("actor", "location", "target", "mentioned"):
                    role = "mentioned"

                link = EventEntityLink(
                    event_id=event_id,
                    entity_id=profile.id,
                    role=role,
                    confidence=0.8,
                )
                event_linked = await structured.save_event_entity_link(link)

                # Update profile event link metadata
                if event_linked:
                    profile.event_link_count += 1
                    profile.last_event_link_at = datetime.now(timezone.utc)
                    await structured.save_entity_profile(profile)
            except ValueError:
                pass  # invalid UUID, skip linking

        result = {
            "resolution": resolution,
            "entity_id": str(profile.id),
            "canonical_name": profile.canonical_name,
            "entity_type": profile.entity_type.value,
            "completeness": profile.completeness_score,
            "event_linked": event_linked,
        }
        if resolution == "existing" and name.lower() != profile.canonical_name.lower():
            result["resolved_from"] = name
            result["note"] = f"'{name}' resolved to canonical '{profile.canonical_name}'"

        return json.dumps(result, indent=2)

    registry.register(ENTITY_PROFILE_DEF, entity_profile_handler)
    registry.register(ENTITY_INSPECT_DEF, entity_inspect_handler)
    registry.register(ENTITY_RESOLVE_DEF, entity_resolve_handler)
