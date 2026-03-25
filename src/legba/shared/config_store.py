"""Versioned config store backed by Postgres.

Stores prompt templates, world briefing, mission config, and guidance addons
as versioned text records. Each update creates a new version; only one version
per key is active at a time. Supports rollback to any previous version.

Schema:
    config_versions(id SERIAL, key TEXT, value TEXT, version INT,
                    created_at TIMESTAMPTZ, created_by TEXT, notes TEXT, active BOOL)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# All config keys that can be stored.
# ---------------------------------------------------------------------------

CONFIG_KEYS = [
    "system_prompt", "plan_prompt", "reflect_prompt", "narrate_prompt",
    "journal_consolidation_prompt", "analysis_report_prompt",
    "survey_prompt", "curate_prompt", "analysis_prompt", "research_prompt",
    "synthesize_prompt", "evolve_prompt", "introspection_prompt",
    "acquire_prompt", "source_discovery_prompt", "bootstrap_addon",
    "reporting_reminder", "liveness_prompt", "reground_prompt",
    "tool_calling_instructions", "memory_management_guidance",
    "efficiency_guidance", "analytics_guidance", "orchestration_guidance",
    "sa_guidance", "situation_guidance", "entity_guidance",
    "goal_context_template", "memory_context_template", "inbox_template",
    "world_briefing", "seed_goal",
]

# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------


async def ensure_config_schema(pool) -> None:
    """Create the config_versions table and indexes if they don't exist."""
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS config_versions (
                id SERIAL PRIMARY KEY,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                version INT NOT NULL DEFAULT 1,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                created_by TEXT DEFAULT 'system',
                notes TEXT DEFAULT '',
                active BOOLEAN DEFAULT TRUE
            );
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_config_active
            ON config_versions (key) WHERE active = TRUE;
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_config_key_version
            ON config_versions (key, version DESC);
        """)


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------


async def get_active(pool, key: str) -> str | None:
    """Fetch the active value for a key, or None if not set."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value FROM config_versions WHERE key = $1 AND active = TRUE",
            key,
        )
        return row["value"] if row else None


async def get_active_with_version(pool, key: str) -> tuple[str | None, int | None]:
    """Fetch (value, version) for the active version, or (None, None)."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value, version FROM config_versions WHERE key = $1 AND active = TRUE",
            key,
        )
        if row:
            return row["value"], row["version"]
        return None, None


async def update(
    pool,
    key: str,
    value: str,
    author: str = "system",
    notes: str = "",
) -> int:
    """Insert a new version for *key*, deactivating any current active version.

    Returns the new version number.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Find the current max version for this key.
            row = await conn.fetchrow(
                "SELECT COALESCE(MAX(version), 0) AS max_v FROM config_versions WHERE key = $1",
                key,
            )
            new_version = row["max_v"] + 1

            # Deactivate current active version (if any).
            await conn.execute(
                "UPDATE config_versions SET active = FALSE WHERE key = $1 AND active = TRUE",
                key,
            )

            # Insert the new version.
            await conn.execute(
                """INSERT INTO config_versions (key, value, version, created_by, notes, active)
                   VALUES ($1, $2, $3, $4, $5, TRUE)""",
                key, value, new_version, author, notes,
            )

    log.info("config_store: updated %s to version %d by %s", key, new_version, author)
    return new_version


async def rollback(pool, key: str, target_version: int) -> bool:
    """Activate *target_version* for *key*, deactivating the current active version.

    Returns True on success, False if target_version does not exist.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Verify target version exists.
            exists = await conn.fetchrow(
                "SELECT id FROM config_versions WHERE key = $1 AND version = $2",
                key, target_version,
            )
            if not exists:
                return False

            # Deactivate current active version.
            await conn.execute(
                "UPDATE config_versions SET active = FALSE WHERE key = $1 AND active = TRUE",
                key,
            )

            # Activate target version.
            await conn.execute(
                "UPDATE config_versions SET active = TRUE WHERE key = $1 AND version = $2",
                key, target_version,
            )

    log.info("config_store: rolled back %s to version %d", key, target_version)
    return True


async def history(pool, key: str, limit: int = 20) -> list[dict[str, Any]]:
    """List versions for a key, newest first.

    Returns a list of dicts with: version, created_at, created_by, notes,
    active, and the first 100 characters of value (preview).
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT version, created_at, created_by, notes, active,
                      LEFT(value, 100) AS preview
               FROM config_versions
               WHERE key = $1
               ORDER BY version DESC
               LIMIT $2""",
            key, limit,
        )
    return [
        {
            "version": r["version"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "created_by": r["created_by"],
            "notes": r["notes"],
            "active": r["active"],
            "preview": r["preview"],
        }
        for r in rows
    ]


async def list_keys(pool) -> list[dict[str, Any]]:
    """List all keys with their active version number and last updated time."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT key, version, created_at
               FROM config_versions
               WHERE active = TRUE
               ORDER BY key""",
        )
    return [
        {
            "key": r["key"],
            "version": r["version"],
            "updated_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]


async def seed_defaults(
    pool,
    defaults: dict[str, str],
    author: str = "seed",
) -> None:
    """Seed default values for keys that have no active version.

    This is idempotent — safe to call on every startup.  Keys that already
    have an active version are left untouched.
    """
    async with pool.acquire() as conn:
        for key, value in defaults.items():
            existing = await conn.fetchrow(
                "SELECT id FROM config_versions WHERE key = $1 AND active = TRUE",
                key,
            )
            if existing:
                continue
            # Check if there are any versions at all (inactive rollback state).
            row = await conn.fetchrow(
                "SELECT COALESCE(MAX(version), 0) AS max_v FROM config_versions WHERE key = $1",
                key,
            )
            new_version = row["max_v"] + 1
            await conn.execute(
                """INSERT INTO config_versions (key, value, version, created_by, notes, active)
                   VALUES ($1, $2, $3, $4, 'initial seed', TRUE)""",
                key, value, new_version, author,
            )
            log.info("config_store: seeded %s as version %d", key, new_version)


# ---------------------------------------------------------------------------
# Default config extraction — maps CONFIG_KEYS to current Python constants
# ---------------------------------------------------------------------------

# Mapping from config key name to templates.py constant name.
_KEY_TO_TEMPLATE: dict[str, str] = {
    "system_prompt": "SYSTEM_PROMPT",
    "plan_prompt": "PLAN_PROMPT",
    "reflect_prompt": "REFLECT_PROMPT",
    "narrate_prompt": "NARRATE_PROMPT",
    "journal_consolidation_prompt": "JOURNAL_CONSOLIDATION_PROMPT",
    "analysis_report_prompt": "ANALYSIS_REPORT_PROMPT",
    "survey_prompt": "SURVEY_PROMPT",
    "curate_prompt": "CURATE_PROMPT",
    "analysis_prompt": "ANALYSIS_PROMPT",
    "research_prompt": "RESEARCH_PROMPT",
    "synthesize_prompt": "SYNTHESIZE_PROMPT",
    "evolve_prompt": "EVOLVE_PROMPT",
    "introspection_prompt": "MISSION_REVIEW_PROMPT",
    "acquire_prompt": "ACQUIRE_PROMPT",
    "source_discovery_prompt": "SOURCE_DISCOVERY_PROMPT",
    "bootstrap_addon": "BOOTSTRAP_PROMPT_ADDON",
    "reporting_reminder": "REPORTING_REMINDER",
    "liveness_prompt": "LIVENESS_PROMPT",
    "reground_prompt": "REGROUND_PROMPT",
    "tool_calling_instructions": "TOOL_CALLING_INSTRUCTIONS",
    "memory_management_guidance": "MEMORY_MANAGEMENT_GUIDANCE",
    "efficiency_guidance": "EFFICIENCY_GUIDANCE",
    "analytics_guidance": "ANALYTICS_GUIDANCE",
    "orchestration_guidance": "ORCHESTRATION_GUIDANCE",
    "sa_guidance": "SA_GUIDANCE",
    "situation_guidance": "SITUATION_GUIDANCE",
    "entity_guidance": "ENTITY_GUIDANCE",
    "goal_context_template": "GOAL_CONTEXT_TEMPLATE",
    "memory_context_template": "MEMORY_CONTEXT_TEMPLATE",
    "inbox_template": "INBOX_TEMPLATE",
}


def _find_seed_goal_dir() -> Path:
    """Locate the seed_goal directory.

    Checks a few conventional paths so this works both inside the agent
    container (/agent/seed_goal) and in the dev tree.
    """
    candidates = [
        Path("/agent/seed_goal"),
        Path(__file__).resolve().parents[3] / "seed_goal",  # src/legba/shared -> repo root
    ]
    for p in candidates:
        if p.is_dir():
            return p
    # Fall back to repo root relative to CWD.
    return Path("seed_goal")


def get_default_configs() -> dict[str, str]:
    """Return a dict mapping every CONFIG_KEY to its current default value.

    Prompt templates are read from templates.py constants.
    world_briefing and seed_goal are read from seed_goal/ files on disk.
    """
    # Lazy import to avoid circular dependencies at module level.
    from legba.agent.prompt import templates

    defaults: dict[str, str] = {}

    # Pull all prompt-template keys from the templates module.
    for config_key, template_attr in _KEY_TO_TEMPLATE.items():
        value = getattr(templates, template_attr, None)
        if value is not None:
            defaults[config_key] = value
        else:
            log.warning("config_store: template constant %s not found in templates.py", template_attr)

    # File-backed keys.
    seed_dir = _find_seed_goal_dir()

    # world_briefing
    wb_path = seed_dir / "world_briefing.txt"
    if wb_path.is_file():
        defaults["world_briefing"] = wb_path.read_text(encoding="utf-8")
    else:
        log.warning("config_store: world_briefing.txt not found at %s", wb_path)
        defaults["world_briefing"] = ""

    # seed_goal — read from goal.txt (the actual seed goal file)
    goal_path = seed_dir / "goal.txt"
    if goal_path.is_file():
        defaults["seed_goal"] = goal_path.read_text(encoding="utf-8")
    else:
        log.warning("config_store: goal.txt not found at %s", goal_path)
        defaults["seed_goal"] = ""

    return defaults
