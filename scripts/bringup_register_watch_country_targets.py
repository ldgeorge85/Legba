# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register HIGH-CONSEQUENCE non-G20 country targets (the `watch` tier).

Companion to ``bringup_register_g20_country_targets.py``. Same source-first
TargetDescriptor shape, but scoped to high-consequence countries OUTSIDE the G20
and tagged ``watch`` (instead of ``g20``). The analyst roster is scoped by a
COVERAGE tag — the four bounded units + country_composition subscribe on
``has_tag("g20") or has_tag("watch")``, and the scorecard enumerates any target
tagged g20/watch — so adding a country here is all it takes for the full spine
(units -> composition -> scorecard -> world composition) to card it.

Operator selection (2026-07-01): Israel, Iran, Ukraine, Taiwan, North Korea.
Add more by extending ``WATCH_ISO2``.

Idempotent; validated against the real TargetDescriptor schema before write.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _p17_registrar import (  # noqa: E402
    Family,
    RegisterResult,
    close_registry,
    open_registry,
    print_results,
    register_descriptor,
)

from legba.data.schemas.target import TargetDescriptor  # noqa: E402

# High-consequence non-G20 countries (ISO-3166-1 alpha-2). Extend to add more.
WATCH_ISO2 = ["IL", "IR", "UA", "TW", "KP"]

# Same geopolitical vocabulary the G20 targets reason over (validated against
# vocabulary_entries).
ENTITY_CLASSES = [
    "country", "organization", "corporation", "person", "location",
    "political_party", "international_org", "infrastructure", "media_outlet",
    "military_unit", "armed_group", "event_series", "commodity", "concept",
]
RELATIONSHIP_TYPES = [
    "AlliedWith", "HostileTo", "MemberOf", "LeaderOf", "TradesWith",
    "BordersWith", "SanctionsAgainst", "DiplomaticRelationsWith",
    "MilitaryPresenceIn", "SuppliesWeaponsTo",
]


async def _country_meta(pg) -> dict[str, dict]:
    """ISO2 -> {name, languages[]} from the iso_countries seed table."""
    async with pg.acquire() as conn:
        rows = await conn.fetch(
            "SELECT iso2, name, languages FROM iso_countries "
            "WHERE iso2 = ANY($1::text[])",
            WATCH_ISO2,
        )
    import json

    out: dict[str, dict] = {}
    for r in rows:
        langs = r["languages"]
        if isinstance(langs, (str, bytes, bytearray)):
            try:
                langs = json.loads(langs)
            except Exception:
                langs = []
        out[r["iso2"]] = {"name": r["name"], "languages": list(langs or [])}
    return out


def _build_target(iso2: str, name: str, languages: list[str]) -> TargetDescriptor:
    iso_lower = iso2.lower()
    langs = list(languages)
    if "en" not in langs:
        langs.append("en")
    body = {
        "identity": {
            "id": f"country_watch_{iso_lower}",
            "name": f"Watch — {name}",
            "schema_uri": "legba/target/2.0.0",
            "version": "0" * 16,
            "abstraction_level": "L1",
            "inherits": [],
            "state": "active",
            "owner": "watch_tier",
            "created": "2026-07-01T00:00:00Z",
        },
        "scope": {
            "domain": "geo",
            "geo": [iso2],
            "languages": langs,
            "entity_classes": ENTITY_CLASSES,
            "relationship_types": RELATIONSHIP_TYPES,
            "time_horizon_days": 90,
            "predicate": None,
            # `watch` is the coverage tag the units + composition subscribe to
            # alongside `g20`; the scorecard enumerates g20/watch targets.
            "tags": ["geopolitical", "news", "watch"],
        },
        "sources": [
            {
                "source_selector": {
                    "tags": ["news"],
                    "kinds": ["rss"],
                    "owner_tenant": "shared",
                },
                "subscription": {
                    "geo": [iso2],
                    "predicate": f'geo_match(["{iso2}"])',
                    "canonical_only": True,
                },
            }
        ],
        "analyst": {
            "use": "inline_target",
            "cadence": {"fallback_schedule": "*/10 * * * *"},
            "method": {
                "kind": "llm_planner",
                "prompt_module": "legba.runtime.analyst_method:_DEFAULT_SYSTEM",
                "budget_tokens_per_day": 200000,
                "llm": {
                    "primary": {
                        "factory_kind": "stack_ref",
                        "raw": "llm.primary.openai_compat",
                        "expected_family": "llm_provider",
                    }
                },
            },
        },
        "allowed_action_packs": [
            {"pack_id": "media_processing"},
            {"pack_id": "incident_response"},
            {"pack_id": "escalate_finding"},
        ],
        "pipeline": {"ingestion_filters": [], "enrichment": [], "routing": []},
        "outputs": [
            {
                "kind": "a2a_skill",
                "config": {"skill_id": f"intelligence.country_watch_{iso_lower}_assessment"},
            }
        ],
    }
    return TargetDescriptor.model_validate(body, strict=False)


async def main() -> int:
    pg, reg = await open_registry()
    try:
        meta = await _country_meta(pg)
        missing = [c for c in WATCH_ISO2 if c not in meta]
        if missing:
            print(f"WARNING: iso_countries missing codes {missing} — "
                  f"falling back to bare codes for those.")

        results: list[RegisterResult] = []
        for iso2 in WATCH_ISO2:
            m = meta.get(iso2, {"name": iso2, "languages": []})
            desc = _build_target(iso2, m["name"], m["languages"])
            results.append(
                await register_descriptor(pg, reg, family=Family.TARGET, descriptor=desc)
            )
        failures = print_results("Watch-tier high-consequence country targets:", results)
    finally:
        await close_registry(pg, reg)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
