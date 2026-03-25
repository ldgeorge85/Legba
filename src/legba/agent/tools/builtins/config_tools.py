"""
Config Tools — read and update versioned config store entries.

Provides config_read and config_update tools for the agent to inspect
and modify prompt templates, guidance text, and mission config through
the versioned config store (Postgres-backed).  Changes are tracked
with version history and audit trail.

Registered in the EVOLVE tool set only — replaces fs_write for prompt
self-modification.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ....shared.schemas.tools import ToolDefinition, ToolParameter

if TYPE_CHECKING:
    from ...memory.structured import StructuredStore
    from ...tools.registry import ToolRegistry
    from ....shared.schemas.cycle import CycleState

logger = logging.getLogger(__name__)


def register(
    registry: ToolRegistry,
    *,
    structured: StructuredStore,
    state: CycleState,
) -> None:
    """Register config_read and config_update tools."""

    def _check_available() -> str | None:
        if structured is None or not structured._available:
            return "Error: Structured store (Postgres) is not available"
        if structured._pool is None:
            return "Error: No database pool available"
        return None

    # ------------------------------------------------------------------
    # config_read
    # ------------------------------------------------------------------

    read_def = ToolDefinition(
        name="config_read",
        description=(
            "Read the active value for a config key from the versioned config store. "
            "Keys include prompt templates (system_prompt, plan_prompt, survey_prompt, "
            "evolve_prompt, etc.), guidance sections, and mission config (world_briefing, "
            "seed_goal). Returns the full text value or 'Key not found.'"
        ),
        parameters=[
            ToolParameter(
                name="key",
                type="string",
                description="The config key to read (e.g. 'system_prompt', 'evolve_prompt', 'sa_guidance')",
            ),
        ],
    )

    async def config_read_handler(args: dict) -> str:
        err = _check_available()
        if err:
            return err

        key = args.get("key", "").strip()
        if not key:
            return "Error: key parameter is required"

        try:
            from ....shared.config_store import get_active
            value = await get_active(structured._pool, key)
            if value is None:
                return "Key not found."
            return value
        except Exception as e:
            logger.error("config_read failed for key=%s: %s", key, e)
            return f"Error reading config: {e}"

    registry.register(read_def, config_read_handler)

    # ------------------------------------------------------------------
    # config_update
    # ------------------------------------------------------------------

    update_def = ToolDefinition(
        name="config_update",
        description=(
            "Update a config key with a new value. Creates a new version in the "
            "versioned config store (previous versions are preserved for rollback). "
            "Use this instead of fs_write for prompt/guidance modifications. "
            "Returns 'Updated {key} to version {n}.' on success."
        ),
        parameters=[
            ToolParameter(
                name="key",
                type="string",
                description="The config key to update (e.g. 'evolve_prompt', 'sa_guidance')",
            ),
            ToolParameter(
                name="value",
                type="string",
                description="The new value for this config key (full text, not a diff)",
            ),
            ToolParameter(
                name="notes",
                type="string",
                description="Brief note explaining what changed and why",
                required=False,
            ),
        ],
    )

    async def config_update_handler(args: dict) -> str:
        err = _check_available()
        if err:
            return err

        key = args.get("key", "").strip()
        value = args.get("value", "")
        notes = args.get("notes", "")

        if not key:
            return "Error: key parameter is required"
        if not value:
            return "Error: value parameter is required (provide the full new text)"

        cycle_number = state.cycle_number if state else 0
        created_by = f"agent:evolve:cycle_{cycle_number}"

        try:
            from ....shared.config_store import update as config_update
            new_version = await config_update(
                structured._pool,
                key,
                value,
                author=created_by,
                notes=notes or "",
            )
            return f"Updated {key} to version {new_version}."
        except Exception as e:
            logger.error("config_update failed for key=%s: %s", key, e)
            return f"Error updating config: {e}"

    registry.register(update_def, config_update_handler)
